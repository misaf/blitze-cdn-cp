"""The engine, the session, and the transaction boundary.

Owns the connection, the process-wide write lock, and the Unit of Work every
store joins.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool, QueuePool

from blitzecdn.core.persistence.schema import DatabaseSchema
from blitzecdn.core.ports.clock import Clock, system_clock

_SQLITE_BUSY_TIMEOUT_MS = 30_000


def _enable_wal(cursor: Any) -> None:
    """Retry startup lock contention within the normal database wait budget."""
    deadline = monotonic() + _SQLITE_BUSY_TIMEOUT_MS / 1000
    while True:
        remaining = max(0, deadline - monotonic())
        cursor.execute(f"PRAGMA busy_timeout = {int(remaining * 1000)}")
        try:
            cursor.execute("PRAGMA journal_mode = WAL").fetchall()
            return
        except sqlite3.OperationalError as exc:
            # SQLite can skip its busy handler when promoting a journal lock.
            # A fresh statement lets competing journal transitions finish.
            if (
                exc.sqlite_errorcode & 0xFF != sqlite3.SQLITE_BUSY
                or monotonic() >= deadline
            ):
                raise
            sleep(min(0.05, max(0, deadline - monotonic())))


def _configure_sqlite(engine: Engine, immediate: ContextVar[bool]) -> None:
    """Make pysqlite behave as this control plane needs.

    ``PRAGMA foreign_keys``, which SQLite ignores per connection unless asked, so
    cascades actually fire. ``isolation_level=None``, so the driver stops opening
    deferred transactions and committing around DDL behind SQLAlchemy. And
    ``BEGIN IMMEDIATE``, so a use case reserves the writer at its boundary and a
    busy database fails before any work, not halfway through it.
    """

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None
        try:
            with closing(dbapi_connection.cursor()) as cursor:
                cursor.execute("PRAGMA foreign_keys = ON")
                _enable_wal(cursor)
                cursor.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
        except BaseException:
            # SQLAlchemy has not adopted this connection until setup succeeds.
            dbapi_connection.close()
            raise

    @event.listens_for(engine, "begin")
    def _on_begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate.get() else "BEGIN")


class Database:
    """The database file: engine, schema, sessions, and the write lock."""

    def __init__(
        self,
        path: Path,
        *,
        pool_connections: bool = False,
        clock: Clock = system_clock,
    ) -> None:
        DatabaseSchema.validate(path)
        #: Every timestamp a store writes comes from here.
        self._clock = clock
        # Per instance, so two databases in one process never share a session.
        self._session: ContextVar[Session | None] = ContextVar(
            f"blitzecdn_session_{id(self)}", default=None
        )
        self._immediate: ContextVar[bool] = ContextVar(
            f"blitzecdn_begin_immediate_{id(self)}", default=False
        )
        # SQLite admits one writer: wait here rather than race to `BEGIN
        # IMMEDIATE` and fail. Reentrant because nested use cases join.
        self.lock = threading.RLock()
        pool_options: dict[str, Any]
        if pool_connections:
            # A small bounded pool for the long-lived API.
            pool_options = {
                "poolclass": QueuePool,
                "pool_size": 5,
                "max_overflow": 5,
                "pool_timeout": 30,
            }
        else:
            # No pool for short-lived CLI processes.
            pool_options = {"poolclass": NullPool}
        self.engine = create_engine(
            URL.create(
                "sqlite+pysqlite",
                database=path.resolve().as_uri(),
                query={"mode": "rw", "uri": "true"},
            ),
            **pool_options,
        )
        _configure_sqlite(self.engine, self._immediate)

    def _new_session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Join the Unit of Work, else commit or roll back this store call."""
        ambient = self._session.get()
        if ambient is not None:
            yield ambient
            return
        with self.lock, self._new_session() as session, session.begin():
            yield session

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Reserve the writer; nested calls share the outer transaction.

        No savepoints: failures must escape the outer scope to roll back.
        """
        if self._session.get() is not None:
            yield
            return
        immediate = self._immediate.set(True)
        try:
            with self.lock, self._new_session() as session, session.begin():
                # `session.begin()` is lazy; asking for the connection starts
                # BEGIN IMMEDIATE at the boundary, not at the first statement.
                session.connection()
                token = self._session.set(session)
                try:
                    yield
                finally:
                    self._session.reset(token)
        finally:
            self._immediate.reset(immediate)

    def require_transaction(self, operation: str) -> None:
        """Refuse a compare-and-swap outside a Unit of Work.

        Only ``BEGIN IMMEDIATE`` makes read-compare-write atomic; outside one, the
        deferred upgrade fails with a snapshot-busy error no timeout waits out, and
        rarely enough to reach production first.
        """
        if self.current_session() is None:
            raise ValueError(
                f"{operation} must run inside a Unit of Work: its "
                "compare-and-swap is atomic only under the BEGIN IMMEDIATE "
                "that transaction() emits"
            )

    def current_session(self) -> Session | None:
        """The open Unit of Work, or ``None``; for asserting a use case joined one."""
        return self._session.get()

    def close(self) -> None:
        """Release engine-owned resources during application shutdown."""
        with self.lock:
            self.engine.dispose()

    def now(self) -> datetime:
        return self._clock()
