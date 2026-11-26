"""Database access with request-scoped transactions.

Postgres (psycopg 3) is used in every cloud environment. SQLite is supported
for local development and the unit tests, using the same SQL with ``%s``
placeholders translated. A request or job opens one transaction with
``Database.transaction()``; every repository call inside it uses that
connection, so account state, monitoring events, webhook outbox rows, audit
records and the idempotency record commit or roll back together.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

_current: contextvars.ContextVar[Connection | None] = contextvars.ContextVar(
    "cri_db_connection", default=None
)


class Connection:
    def __init__(self, raw: Any, dialect: str) -> None:
        self.raw = raw
        self.dialect = dialect

    def _sql(self, sql: str) -> str:
        if self.dialect == "sqlite":
            sql = sql.replace("%s", "?")
            sql = re.sub(r"\s+FOR UPDATE\b", "", sql)
        return sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        cur = self.raw.cursor()
        cur.execute(self._sql(sql), tuple(params))
        return cur

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        cur = self.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row, strict=True))

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cur = self.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def set_tenant(self, tenant_id: str | None) -> None:
        """Bind the tenant for Postgres row-level security for this transaction."""
        if self.dialect == "postgres":
            self.execute("SELECT set_config('cri.tenant_id', %s, true)", (tenant_id or "",))

    def advisory_lock(self, key: str) -> None:
        if self.dialect == "postgres":
            self.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        if url.startswith("sqlite:"):
            self.dialect = "sqlite"
            path = url.split("sqlite:///", 1)[-1] or ":memory:"
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._sqlite_path = path
            self._sqlite_lock = threading.RLock()
            self._sqlite_conn = sqlite3.connect(
                path, check_same_thread=False, isolation_level=None
            )
            self._sqlite_conn.execute("PRAGMA foreign_keys=ON")
            self._pool = None
        elif url.startswith(("postgres://", "postgresql://")):
            self.dialect = "postgres"
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                url, min_size=1, max_size=8, kwargs={"autocommit": False}, open=True
            )
        else:
            raise ValueError("CRI_DATABASE_URL must be a sqlite:/// or postgresql:// URL")

    @contextlib.contextmanager
    def transaction(self, tenant_id: str | None = None) -> Iterator[Connection]:
        existing = _current.get()
        if existing is not None:
            # Nested use joins the outer transaction.
            yield existing
            return
        if self.dialect == "sqlite":
            with self._sqlite_lock:
                conn = Connection(self._sqlite_conn, "sqlite")
                self._sqlite_conn.execute("BEGIN IMMEDIATE")
                token = _current.set(conn)
                try:
                    yield conn
                    self._sqlite_conn.execute("COMMIT")
                except BaseException:
                    self._sqlite_conn.execute("ROLLBACK")
                    raise
                finally:
                    _current.reset(token)
            return
        assert self._pool is not None
        with self._pool.connection() as raw:
            conn = Connection(raw, "postgres")
            token = _current.set(conn)
            try:
                conn.set_tenant(tenant_id)
                yield conn
                raw.commit()
            except BaseException:
                raw.rollback()
                raise
            finally:
                _current.reset(token)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
        else:
            self._sqlite_conn.close()


def current_connection() -> Connection:
    conn = _current.get()
    if conn is None:
        raise RuntimeError("no active database transaction; wrap the call in Database.transaction()")
    return conn


def has_transaction() -> bool:
    return _current.get() is not None
