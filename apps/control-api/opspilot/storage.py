from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .knowledge import IncidentMatch, RetrievalScore, RunbookMatch
from .models import IncidentState


class IncidentStore:
    """Small SQLite audit store for the local MVP."""

    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self):
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY, alert_key TEXT UNIQUE, service TEXT NOT NULL,
                    symptom TEXT NOT NULL, status TEXT NOT NULL, root_cause TEXT,
                    confidence REAL NOT NULL DEFAULT 0, state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY, incident_id TEXT NOT NULL, position INTEGER NOT NULL,
                    source TEXT NOT NULL, summary TEXT NOT NULL, data_json TEXT,
                    UNIQUE(incident_id, position)
                );
                CREATE TABLE IF NOT EXISTS agent_events (
                    id INTEGER PRIMARY KEY, incident_id TEXT NOT NULL, position INTEGER NOT NULL,
                    agent TEXT NOT NULL, message TEXT NOT NULL, at TEXT NOT NULL,
                    UNIQUE(incident_id, position)
                );
                CREATE TABLE IF NOT EXISTS recommendations (
                    id INTEGER PRIMARY KEY, incident_id TEXT NOT NULL, position INTEGER NOT NULL,
                    title TEXT NOT NULL, command TEXT, risk TEXT NOT NULL, requires_approval INTEGER NOT NULL,
                    UNIQUE(incident_id, position)
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY, incident_id TEXT NOT NULL, approved INTEGER NOT NULL,
                    requested_execution INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS executions (
                    id INTEGER PRIMARY KEY, incident_id TEXT NOT NULL, command TEXT,
                    result TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS verifications (
                    id INTEGER PRIMARY KEY, incident_id TEXT NOT NULL, verified INTEGER,
                    result TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS policy_decisions (
                    id INTEGER PRIMARY KEY, incident_id TEXT NOT NULL, position INTEGER NOT NULL,
                    allowed INTEGER NOT NULL, reason TEXT NOT NULL, policy TEXT NOT NULL,
                    operation TEXT, target TEXT, command TEXT, created_at TEXT NOT NULL,
                    UNIQUE(incident_id, position)
                );
                CREATE TABLE IF NOT EXISTS runbooks (
                    runbook_id TEXT PRIMARY KEY, title TEXT NOT NULL, service TEXT NOT NULL,
                    root_cause TEXT NOT NULL, description TEXT NOT NULL, command TEXT,
                    verification TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS verification_policy_revisions (
                    id INTEGER PRIMARY KEY, revision INTEGER, content_digest TEXT NOT NULL,
                    signature_status TEXT NOT NULL, load_result TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS verification_policy_peer_credentials (
                    credential_id TEXT PRIMARY KEY, identity_subject TEXT NOT NULL,
                    expires_at INTEGER NOT NULL, consumed_at TEXT NOT NULL
                );
            """)
            self._seed_runbooks(db)

    def consume_verification_policy_peer_credential(
        self, credential_id: str, identity_subject: str, expires_at: int
    ) -> None:
        now = int(datetime.now(timezone.utc).timestamp())
        try:
            with self.connection() as db:
                db.execute(
                    "DELETE FROM verification_policy_peer_credentials WHERE expires_at < ?",
                    (now - 60,),
                )
                db.execute(
                    """INSERT INTO verification_policy_peer_credentials(
                        credential_id, identity_subject, expires_at, consumed_at
                    ) VALUES(?,?,?,?)""",
                    (
                        credential_id,
                        identity_subject,
                        expires_at,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("peer credential has already been used") from exc

    def record_verification_policy_revision(
        self,
        revision: int | None,
        content_digest: str,
        signature_status: str,
        load_result: str,
    ) -> None:
        with self.connection() as db:
            db.execute(
                """INSERT INTO verification_policy_revisions(
                    revision, content_digest, signature_status, load_result, observed_at
                ) VALUES(?,?,?,?,?)""",
                (revision, content_digest, signature_status, load_result,
                 datetime.now(timezone.utc).isoformat()),
            )

    def latest_accepted_verification_policy_revision(self) -> tuple[int, str] | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT revision, content_digest FROM verification_policy_revisions
                   WHERE revision IS NOT NULL AND signature_status = 'valid'
                     AND load_result = 'accepted'
                   ORDER BY revision DESC, id DESC LIMIT 1"""
            ).fetchone()
        return (row["revision"], row["content_digest"]) if row else None

    def _seed_runbooks(self, db):
        now = datetime.now(timezone.utc).isoformat()
        runbooks = (
            (
                "redis-dependency-recovery-v1", "Restart Redis and verify dependency metrics", "*",
                "Redis dependency is unavailable",
                "Restart the allowlisted Redis container, then verify container, service health, and dependency metrics.",
                "docker compose restart redis", "redis container running; service healthy; dependency_up=1",
            ),
            (
                "mysql-dependency-recovery-v1", "Restart MySQL and verify dependency metrics", "*",
                "MySQL dependency is unavailable",
                "Restart the allowlisted MySQL container, then verify container, service health, and dependency metrics.",
                "docker compose restart mysql", "mysql container running; service healthy; dependency_up=1",
            ),
            (
                "service-degradation-recovery-v1", "Restart affected service and verify dependency metrics", "*",
                "Insufficient evidence; dependency or application degradation suspected",
                "Restart only the affected allowlisted service after approval and verify its health and dependency metrics.",
                None, "service container running; service healthy; dependency metrics up",
            ),
        )
        db.executemany(
            """INSERT INTO runbooks(
                runbook_id, title, service, root_cause, description, command, verification,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(runbook_id) DO NOTHING""",
            [(*runbook, now, now) for runbook in runbooks],
        )

    def save(self, state: IncidentState, alert_key: str | None = None, approved: bool | None = None):
        now = datetime.now(timezone.utc).isoformat()
        payload = state.model_dump_json()
        with self.connection() as db:
            existing = db.execute(
                "SELECT created_at, alert_key FROM incidents WHERE incident_id = ?", (state.incident_id,)
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            effective_key = alert_key if alert_key is not None else (existing["alert_key"] if existing else None)
            db.execute("""
                INSERT INTO incidents(incident_id, alert_key, service, symptom, status, root_cause,
                    confidence, state_json, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(incident_id) DO UPDATE SET alert_key=excluded.alert_key,
                    service=excluded.service, symptom=excluded.symptom, status=excluded.status,
                    root_cause=excluded.root_cause, confidence=excluded.confidence,
                    state_json=excluded.state_json, updated_at=excluded.updated_at
            """, (state.incident_id, effective_key, state.service, state.symptom, state.status,
                  state.root_cause, state.confidence, payload, created_at, now))
            for table in ("evidence", "agent_events", "recommendations", "policy_decisions"):
                db.execute(f"DELETE FROM {table} WHERE incident_id = ?", (state.incident_id,))
            db.executemany(
                "INSERT INTO evidence(incident_id, position, source, summary, data_json) VALUES(?,?,?,?,?)",
                [(state.incident_id, i, item.source, item.summary, json.dumps(item.data))
                 for i, item in enumerate(state.evidence)],
            )
            db.executemany(
                "INSERT INTO agent_events(incident_id, position, agent, message, at) VALUES(?,?,?,?,?)",
                [(state.incident_id, i, item.agent.value, item.message, item.at.isoformat())
                 for i, item in enumerate(state.events)],
            )
            db.executemany(
                "INSERT INTO recommendations(incident_id, position, title, command, risk, requires_approval) VALUES(?,?,?,?,?,?)",
                [(state.incident_id, i, item.title, item.command, item.risk.value, int(item.requires_approval))
                 for i, item in enumerate(state.recommendations)],
            )
            policy_records = [item.data for item in state.evidence if item.source == "execution_policy"]
            db.executemany(
                """INSERT INTO policy_decisions(
                    incident_id, position, allowed, reason, policy, operation, target, command, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                [(
                    state.incident_id, i, int(item["allowed"]), item["reason"], item["policy"],
                    item.get("operation"), item.get("target"), item.get("command"), now,
                ) for i, item in enumerate(policy_records)],
            )
            if approved is not None:
                db.execute("INSERT INTO approvals(incident_id, approved, requested_execution, created_at) VALUES(?,?,?,?)",
                           (state.incident_id, int(approved), int(state.execution_requested), now))
            if state.execution_result is not None:
                command = state.recommendations[0].command if state.recommendations else None
                db.execute("INSERT INTO executions(incident_id, command, result, created_at) VALUES(?,?,?,?)",
                           (state.incident_id, command, state.execution_result, now))
            if state.verified is not None:
                db.execute("INSERT INTO verifications(incident_id, verified, result, created_at) VALUES(?,?,?,?)",
                           (state.incident_id, int(state.verified), state.status, now))

    def get(self, incident_id: str) -> IncidentState | None:
        with self.connection() as db:
            row = db.execute("SELECT state_json FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()
        return IncidentState.model_validate_json(row["state_json"]) if row else None

    def get_by_alert_key(self, alert_key: str) -> IncidentState | None:
        with self.connection() as db:
            row = db.execute("SELECT state_json FROM incidents WHERE alert_key = ?", (alert_key,)).fetchone()
        return IncidentState.model_validate_json(row["state_json"]) if row else None

    def list(self, limit: int = 50, offset: int = 0) -> list[IncidentState]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT state_json FROM incidents ORDER BY updated_at DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return [IncidentState.model_validate_json(row["state_json"]) for row in rows]

    def retrieve_runbooks(
        self, service: str, symptom: str, root_cause: str, limit: int = 3
    ) -> list[RunbookMatch]:
        """Rank enabled runbooks deterministically; exact root cause is the strongest signal."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM runbooks WHERE enabled = 1 AND service IN (?, '*')",
                (service,),
            ).fetchall()
        symptom_terms = set(symptom.lower().split())
        ranked: list[RunbookMatch] = []
        for row in rows:
            factors = {
                "root_cause_match": 100.0 if row["root_cause"] == root_cause else 0.0,
                "service_match": 20.0 if row["service"] == service else 10.0,
                "symptom_match": float(len(
                    symptom_terms & set((row["title"] + " " + row["description"]).lower().split())
                )),
            }
            total = sum(factors.values())
            if factors["root_cause_match"] == 0:
                continue
            ranked.append(RunbookMatch(
                runbook_id=row["runbook_id"], title=row["title"], service=row["service"],
                root_cause=row["root_cause"], description=row["description"],
                command=row["command"], verification=row["verification"],
                score=total, score_explanation=RetrievalScore(total=total, factors=factors),
            ))
        ranked.sort(key=lambda item: (-item.score, item.runbook_id))
        return ranked[:limit]

    def list_runbooks(self) -> list[RunbookMatch]:
        """Return the enabled corpus for optional semantic ranking."""
        with self.connection() as db:
            rows = db.execute("SELECT * FROM runbooks WHERE enabled = 1 ORDER BY runbook_id").fetchall()
        return [RunbookMatch(
            runbook_id=row["runbook_id"], title=row["title"], service=row["service"],
            root_cause=row["root_cause"], description=row["description"], command=row["command"],
            verification=row["verification"], score=0,
            score_explanation=RetrievalScore(total=0, factors={}),
        ) for row in rows]

    def retrieve_incidents(self, incident: IncidentState, limit: int = 3) -> list[IncidentMatch]:
        """Return compact similar-incident summaries without recursively embedding snapshots."""
        with self.connection() as db:
            rows = db.execute(
                """SELECT incident_id, service, symptom, status, root_cause, confidence, state_json, updated_at
                   FROM incidents
                   WHERE incident_id != ? AND service = ? AND root_cause = ?""",
                (incident.incident_id, incident.service, incident.root_cause),
            ).fetchall()
        results: list[IncidentMatch] = []
        symptom_terms = set(incident.symptom.lower().split())
        for row in rows:
            previous = IncidentState.model_validate_json(row["state_json"])
            factors = {
                "verified_outcome": 40.0 if previous.verified is True else 0.0,
                "resolved_status": 30.0 if row["status"] == "resolved" else 0.0,
                "service_match": 20.0,
                "root_cause_match": 20.0,
                "symptom_match": float(len(symptom_terms & set(row["symptom"].lower().split()))),
            }
            results.append(IncidentMatch(
                incident_id=row["incident_id"], service=row["service"], symptom=row["symptom"],
                status=row["status"], root_cause=row["root_cause"], confidence=row["confidence"],
                recommendations=[item.model_dump(mode="json") for item in previous.recommendations],
                execution_result=previous.execution_result, verified=previous.verified,
                updated_at=row["updated_at"],
                score=sum(factors.values()),
                score_explanation=RetrievalScore(total=sum(factors.values()), factors=factors),
            ))
        # Stable two-pass ordering: newest wins ties, outcome quality wins overall.
        results.sort(key=lambda item: item.updated_at, reverse=True)
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:limit]
