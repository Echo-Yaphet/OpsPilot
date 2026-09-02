import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class ReplayError(ValueError):
    pass


class RuntimeStore:
    """Atomic replay consumption and audit storage for one or many executors."""

    def __init__(self, *, database_path: str, database_url: str | None = None):
        self.database_path = database_path
        self.database_url = database_url or ""
        self.shared = bool(self.database_url)
        self._initialize()

    @contextmanager
    def _connect(self):
        if self.shared:
            import psycopg

            with psycopg.connect(self.database_url) as connection:
                yield connection
            return
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            yield connection

    def _initialize(self) -> None:
        with self._connect() as db:
            if self.shared:
                # PostgreSQL can still race in its system catalog when two fresh
                # replicas issue CREATE TABLE IF NOT EXISTS at the same instant.
                db.execute("SELECT pg_advisory_xact_lock(%s)", (1_867_754_921,))
                db.execute("""CREATE TABLE IF NOT EXISTS runtime_consumed_credentials (
                    credential_id TEXT PRIMARY KEY, identity_subject TEXT NOT NULL,
                    expires_at BIGINT NOT NULL, consumed_at BIGINT NOT NULL,
                    placement TEXT NOT NULL, executor_id TEXT NOT NULL
                )""")
                db.execute("""CREATE TABLE IF NOT EXISTS runtime_audit (
                    id BIGSERIAL PRIMARY KEY, operation TEXT NOT NULL, target TEXT NOT NULL,
                    outcome TEXT NOT NULL, detail TEXT NOT NULL, identity_subject TEXT,
                    credential_id TEXT, identity_key_id TEXT, placement TEXT NOT NULL,
                    executor_id TEXT NOT NULL, created_at TEXT NOT NULL
                )""")
            else:
                db.execute("""CREATE TABLE IF NOT EXISTS consumed_credentials (
                    credential_id TEXT PRIMARY KEY, identity_subject TEXT NOT NULL,
                    expires_at INTEGER NOT NULL, consumed_at INTEGER NOT NULL
                )""")
                db.execute("""CREATE TABLE IF NOT EXISTS runtime_audit (
                    id INTEGER PRIMARY KEY, operation TEXT NOT NULL, target TEXT NOT NULL,
                    outcome TEXT NOT NULL, detail TEXT NOT NULL, identity_subject TEXT,
                    credential_id TEXT, identity_key_id TEXT, created_at TEXT NOT NULL
                )""")
                columns = {row[1] for row in db.execute("PRAGMA table_info(runtime_audit)")}
                if "placement" not in columns:
                    db.execute("ALTER TABLE runtime_audit ADD COLUMN placement TEXT NOT NULL DEFAULT 'local-compose'")
                if "executor_id" not in columns:
                    db.execute("ALTER TABLE runtime_audit ADD COLUMN executor_id TEXT NOT NULL DEFAULT 'runtime-executor-local'")

    def consume(self, identity: dict, *, placement: str, executor_id: str) -> None:
        now = int(time.time())
        table = "runtime_consumed_credentials" if self.shared else "consumed_credentials"
        try:
            with self._connect() as db:
                placeholder = "%s" if self.shared else "?"
                db.execute(f"DELETE FROM {table} WHERE expires_at < {placeholder}", (now - 60,))
                if self.shared:
                    db.execute(
                        """INSERT INTO runtime_consumed_credentials(
                            credential_id,identity_subject,expires_at,consumed_at,placement,executor_id
                        ) VALUES(%s,%s,%s,%s,%s,%s)""",
                        (identity["jti"], identity["sub"], identity["exp"], now, placement, executor_id),
                    )
                else:
                    db.execute(
                        "INSERT INTO consumed_credentials VALUES(?,?,?,?)",
                        (identity["jti"], identity["sub"], identity["exp"], now),
                    )
        except Exception as exc:
            if self._is_unique_violation(exc):
                raise ReplayError("credential has already been used") from exc
            raise

    def audit(
        self, operation: str, target: str, outcome: str, detail: str, identity: dict,
        *, placement: str, executor_id: str,
    ) -> None:
        values = (
            operation, target, outcome, detail, identity.get("sub"), identity.get("jti"),
            identity.get("key_id"), placement, executor_id, datetime.now(timezone.utc).isoformat(),
        )
        with self._connect() as db:
            placeholders = ",".join(["%s" if self.shared else "?"] * len(values))
            db.execute(
                "INSERT INTO runtime_audit(operation,target,outcome,detail,identity_subject,"
                "credential_id,identity_key_id,placement,executor_id,created_at) "
                f"VALUES({placeholders})",
                values,
            )

    @staticmethod
    def _is_unique_violation(exc: Exception) -> bool:
        if isinstance(exc, sqlite3.IntegrityError):
            return True
        try:
            from psycopg.errors import UniqueViolation

            return isinstance(exc, UniqueViolation)
        except ImportError:
            return False
