"""Filtered PostgreSQL/pgvector event memory for investigation context."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def apply_postgres_migration(connection, version: int, statements: list[str]) -> None:
    """Apply one atomic migration; no version or partial DDL survives failure."""
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(675091743)")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS opspilot_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL)"
        )
        applied = connection.execute(
            "SELECT 1 FROM opspilot_schema_migrations WHERE version=%s", (version,)
        ).fetchone()
        if applied:
            return
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO opspilot_schema_migrations(version, applied_at) VALUES(%s, now())",
            (version,),
        )


class PostgresEventMemory:
    """Stores lifecycle events and applies metadata filters before vector ranking."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._migrate()

    def connection(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row)

    def _migrate(self) -> None:
        # The advisory lock serializes startup across primary/canary processes. All
        # statements share one transaction, so a failed migration rolls back cleanly.
        with self.connection() as db:
            apply_postgres_migration(db, 1, [
                "CREATE EXTENSION IF NOT EXISTS vector",
                (
                    "CREATE TABLE IF NOT EXISTS event_memories ("
                    "event_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, service TEXT NOT NULL, "
                    "service_version TEXT NOT NULL, conditions JSONB NOT NULL, category TEXT NOT NULL, "
                    "content JSONB NOT NULL, outcome JSONB, embedding VECTOR, "
                    "created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL)"
                ),
                (
                    "CREATE INDEX IF NOT EXISTS event_memories_filter_idx ON event_memories "
                    "(service, service_version, expires_at)"
                ),
            ])

    @staticmethod
    def _vector(values: list[float] | None) -> str | None:
        if values is None:
            return None
        return "[" + ",".join(format(float(value), ".12g") for value in values) + "]"

    def remember(
        self,
        *,
        event_id: str,
        incident_id: str,
        service: str,
        service_version: str,
        conditions: dict[str, Any],
        category: str,
        content: dict[str, Any],
        expires_at: datetime,
        outcome: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.connection() as db:
            db.execute(
                "INSERT INTO event_memories(event_id, incident_id, service, service_version, "
                "conditions, category, content, outcome, embedding, created_at, expires_at) "
                "VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s::jsonb,%s::vector,%s,%s) "
                "ON CONFLICT(event_id) DO UPDATE SET content=excluded.content, "
                "outcome=excluded.outcome, embedding=excluded.embedding, expires_at=excluded.expires_at",
                (event_id, incident_id, service, service_version, json.dumps(conditions), category,
                 json.dumps(content), json.dumps(outcome) if outcome is not None else None,
                 self._vector(embedding), now, expires_at),
            )

    def search(
        self,
        *,
        service: str,
        service_version: str,
        conditions: dict[str, Any],
        query_embedding: list[float] | None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Filter scope and TTL in the CTE before applying pgvector distance."""
        ordering = "embedding <=> %s::vector, created_at DESC" if query_embedding else "created_at DESC"
        parameters: list[Any] = [service, service_version, json.dumps(conditions)]
        if query_embedding:
            parameters.append(self._vector(query_embedding))
        parameters.append(limit)
        with self.connection() as db:
            rows = db.execute(
                "WITH filtered AS (SELECT event_id, incident_id, category, content, outcome, "
                "created_at, embedding FROM event_memories WHERE service=%s AND service_version=%s "
                "AND conditions <@ %s::jsonb AND expires_at > now()) "
                f"SELECT event_id, incident_id, category, content, outcome, created_at FROM filtered "
                f"ORDER BY {ordering} LIMIT %s",
                parameters,
            ).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            created_at = result.get("created_at")
            if isinstance(created_at, datetime):
                result["created_at"] = created_at.isoformat()
            results.append(result)
        return results

    def health(self) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                "SELECT extversion FROM pg_extension WHERE extname='vector'"
            ).fetchone()
            count = db.execute(
                "SELECT count(*) AS count FROM event_memories WHERE expires_at > now()"
            ).fetchone()["count"]
        return {"backend": "postgresql+pgvector", "healthy": bool(row),
                "pgvector_version": row["extversion"] if row else None,
                "active_events": count}
