from contextlib import contextmanager
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from opspilot.event_memory import PostgresEventMemory, apply_postgres_migration


class FakeResult:
    def fetchall(self):
        return [{"event_id": "one", "incident_id": "incident", "category": "tool_observation",
                 "content": {}, "outcome": None,
                 "created_at": datetime(2026, 9, 8, tzinfo=timezone.utc)}]


class FakeConnection:
    def __init__(self):
        self.query = None
        self.parameters = None

    def execute(self, query, parameters):
        self.query = query
        self.parameters = parameters
        return FakeResult()


def test_event_memory_filters_scope_and_expiry_before_pgvector_ranking():
    memory = object.__new__(PostgresEventMemory)
    connection = FakeConnection()

    @contextmanager
    def connect():
        yield connection

    memory.connection = connect
    rows = memory.search(
        service="payment-service", service_version="v7",
        conditions={"topology": "compose"}, query_embedding=[1.0, 0.0], limit=3,
    )

    assert rows[0]["event_id"] == "one"
    assert connection.query.index("WHERE service=%s") < connection.query.index("ORDER BY embedding")
    assert "conditions <@ %s::jsonb" in connection.query
    assert "expires_at > now()" in connection.query
    assert connection.parameters[:3] == ["payment-service", "v7", '{"topology": "compose"}']
    assert rows[0]["created_at"] == "2026-09-08T00:00:00+00:00"


def test_vector_serialization_is_stable():
    assert PostgresEventMemory._vector([1, 0.25, -0.0]) == "[1,0.25,-0]"


def test_live_pgvector_schema_and_transactional_migration_rollback():
    dsn = os.getenv("MEMORY_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL event memory is not configured")
    memory = PostgresEventMemory(dsn)
    assert memory.health()["healthy"] is True

    with memory.connection() as db:
        db.execute("DROP TABLE IF EXISTS stage3_failed_migration_probe")
        db.execute("DELETE FROM opspilot_schema_migrations WHERE version=999999")
    with memory.connection() as db:
        with pytest.raises(Exception):
            apply_postgres_migration(db, 999999, [
                "CREATE TABLE stage3_failed_migration_probe(id INTEGER)",
                "THIS IS NOT VALID SQL",
            ])
    with memory.connection() as db:
        assert db.execute(
            "SELECT to_regclass('stage3_failed_migration_probe') AS table_name"
        ).fetchone()["table_name"] is None
        assert db.execute(
            "SELECT 1 FROM opspilot_schema_migrations WHERE version=999999"
        ).fetchone() is None


def test_live_event_memory_filters_before_vector_ranking():
    dsn = os.getenv("MEMORY_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL event memory is not configured")
    memory = PostgresEventMemory(dsn)
    prefix = str(uuid4())
    common = {
        "incident_id": prefix, "service_version": prefix,
        "conditions": {"fault": "redis"}, "category": "tool_observation",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    memory.remember(event_id=f"{prefix}-match", service="payment-service",
                    content={"result": "redis down"}, embedding=[1.0, 0.0], **common)
    memory.remember(event_id=f"{prefix}-wrong-service", service="order-service",
                    content={"result": "closer but wrong scope"}, embedding=[1.0, 0.0], **common)
    expired = dict(common)
    expired["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    memory.remember(event_id=f"{prefix}-expired", service="payment-service",
                    content={"result": "expired"}, embedding=[1.0, 0.0], **expired)

    rows = memory.search(
        service="payment-service", service_version=prefix,
        conditions={"fault": "redis", "topology": "compose"},
        query_embedding=[1.0, 0.0], limit=5,
    )

    assert [row["event_id"] for row in rows] == [f"{prefix}-match"]
