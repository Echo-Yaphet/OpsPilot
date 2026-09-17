import asyncio
import sys
import threading
import time
import types

import httpx
import pytest

from opspilot import main
from opspilot.event_memory import PostgresEventMemory
from opspilot.investigation import PostgresInvestigationJournal
from opspilot.models import AnalyzeRequest, IncidentState
from opspilot.storage import DatabaseBusyError, PostgresIncidentStore, run_database_call


class _FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


def _store_without_bootstrap(**overrides):
    store = PostgresIncidentStore.__new__(PostgresIncidentStore)
    store.dsn = "postgresql://example.invalid/opspilot"
    store.path = store.dsn
    store.connect_timeout_seconds = overrides.get("connect_timeout_seconds", 1)
    store.acquire_timeout_seconds = overrides.get("acquire_timeout_seconds", 0.05)
    store.statement_timeout_milliseconds = overrides.get("statement_timeout_milliseconds", 1500)
    store.lock_timeout_milliseconds = overrides.get("lock_timeout_milliseconds", 500)
    store.idle_transaction_timeout_milliseconds = overrides.get(
        "idle_transaction_timeout_milliseconds", 2000
    )
    store._connection_slots = threading.BoundedSemaphore(overrides.get("max_concurrency", 1))
    return store


def test_postgres_connection_applies_explicit_timeouts(monkeypatch):
    captured = {}

    def connect(dsn, **kwargs):
        captured.update({"dsn": dsn, **kwargs})
        return _FakeConnection()

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        types.SimpleNamespace(connect=connect, rows=types.SimpleNamespace(dict_row=object())),
    )
    monkeypatch.setitem(sys.modules, "psycopg.rows", types.SimpleNamespace(dict_row=object()))
    store = _store_without_bootstrap()

    with store.connection():
        pass

    assert captured["connect_timeout"] == 1
    assert "statement_timeout=1500" in captured["options"]
    assert "lock_timeout=500" in captured["options"]
    assert "idle_in_transaction_session_timeout=2000" in captured["options"]


def test_postgres_connection_acquisition_fails_fast_when_capacity_is_full(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        types.SimpleNamespace(connect=lambda *_args, **_kwargs: _FakeConnection()),
    )
    monkeypatch.setitem(sys.modules, "psycopg.rows", types.SimpleNamespace(dict_row=object()))
    store = _store_without_bootstrap(acquire_timeout_seconds=0.04)

    with store.connection():
        started = time.monotonic()
        with pytest.raises(DatabaseBusyError, match="database concurrency capacity"):
            with store.connection():
                pass

    assert time.monotonic() - started < 0.2


@pytest.mark.asyncio
async def test_expired_connect_never_runs_query_after_request_timeout(monkeypatch):
    body_ran = threading.Event()

    def connect(*_args, **_kwargs):
        time.sleep(0.2)
        return _FakeConnection()

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        types.SimpleNamespace(connect=connect, rows=types.SimpleNamespace(dict_row=object())),
    )
    monkeypatch.setitem(sys.modules, "psycopg.rows", types.SimpleNamespace(dict_row=object()))
    store = _store_without_bootstrap()

    def database_operation():
        with store.connection():
            body_ran.set()

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await run_database_call(database_operation, timeout_seconds=0.05)
    assert time.monotonic() - started < 0.15

    await asyncio.sleep(0.25)
    assert body_ran.is_set() is False


@pytest.mark.asyncio
async def test_expired_operation_aborts_before_connection_context_can_commit(monkeypatch):
    class RecordingConnection(_FakeConnection):
        exit_type = None

        def __exit__(self, exc_type, *_):
            self.exit_type = exc_type

    connection = RecordingConnection()
    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        types.SimpleNamespace(
            connect=lambda *_args, **_kwargs: connection,
            rows=types.SimpleNamespace(dict_row=object()),
        ),
    )
    monkeypatch.setitem(sys.modules, "psycopg.rows", types.SimpleNamespace(dict_row=object()))
    store = _store_without_bootstrap()

    def slow_transaction():
        with store.connection():
            time.sleep(0.15)

    with pytest.raises(TimeoutError):
        await run_database_call(slow_transaction, timeout_seconds=0.05)
    await asyncio.sleep(0.2)

    assert connection.exit_type is DatabaseBusyError


@pytest.mark.parametrize("store_type", [PostgresEventMemory, PostgresInvestigationJournal])
def test_auxiliary_postgres_stores_use_the_same_connection_budget(monkeypatch, store_type):
    captured = {}

    def connect(dsn, **kwargs):
        captured.update({"dsn": dsn, **kwargs})
        return _FakeConnection()

    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=connect))
    monkeypatch.setitem(sys.modules, "psycopg.rows", types.SimpleNamespace(dict_row=object()))
    store = store_type.__new__(store_type)
    store.dsn = "postgresql://example.invalid/opspilot"
    store.connect_timeout_seconds = 1
    store.acquire_timeout_seconds = 0.05
    store.statement_timeout_milliseconds = 1500
    store.lock_timeout_milliseconds = 500
    store.idle_transaction_timeout_milliseconds = 2000
    store._connection_slots = threading.BoundedSemaphore(1)

    with store.connection():
        pass

    assert captured["connect_timeout"] == 1
    assert "statement_timeout=1500" in captured["options"]


class _FastWorkflow:
    async def run(self, request: AnalyzeRequest):
        return IncidentState(
            incident_id=request.incident_id or "database-isolation-test",
            service=request.service,
            symptom=request.symptom,
            status="recommendation_ready",
        )


class _SlowStore:
    def save(self, state, **_kwargs):
        time.sleep(0.4)
        raise TimeoutError("database unavailable")


@pytest.mark.asyncio
async def test_database_failure_does_not_block_read_only_health(monkeypatch):
    monkeypatch.setattr(main, "workflow", _FastWorkflow())
    monkeypatch.setattr(main, "store", _SlowStore())
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        failed_write = asyncio.create_task(client.post("/api/v1/incidents/analyze", json={
            "incident_id": "database-isolation-test",
            "service": "payment-service",
            "symptom": "database unavailable",
            "execute": False,
            "approved": False,
        }))

        async def health_after_write_started():
            await asyncio.sleep(0.05)
            return await client.get("/health")

        started = time.monotonic()
        health = await health_after_write_started()
        health_elapsed = time.monotonic() - started
        write = await failed_write

    assert health.status_code == 200
    assert health_elapsed < 0.2
    assert write.status_code == 502
