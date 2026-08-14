"""The engine, the session, and the transaction boundary.

Owns the three things that were hand-rolled around ``sqlite3`` before: the
connection, the process-wide write lock, and the Unit of Work every store
joins. The behaviour is deliberately the same; only the machinery underneath
changed.
"""

from __future__ import annotations

import fcntl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy import Engine, event
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool, QueuePool
from sqlmodel import Session, create_engine

from blitzecdn.exceptions import ConfigurationError


def _configure_sqlite(engine: Engine, immediate: ContextVar[bool]) -> None:
    """Make pysqlite behave the way this control plane needs it to.

    Three separate corrections, none of them optional:

    ``PRAGMA foreign_keys`` — SQLite ignores foreign keys unless asked, per
    connection. Without this the ``ON DELETE CASCADE`` from a zone to its
    records is decoration, and deleting a domain leaves records deriving
    virtual hosts for a zone we no longer serve.

    Disabling the driver's implicit BEGIN — pysqlite opens a *deferred*
    transaction on the first write and, worse, commits behind SQLAlchemy's back
    around DDL. Setting ``isolation_level=None`` on connect hands transaction
    control to us. This is the documented pysqlite workaround, not a trick.

    Emitting ``BEGIN IMMEDIATE`` ourselves — a deferred transaction takes the
    write lock at the first write, which means a multi-store use case can get
    halfway through and *then* discover a competing writer. IMMEDIATE reserves
    the writer at the boundary, so a busy database fails where it can still be
    retried cleanly rather than partway through the work.
    """

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA busy_timeout = 30000")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate.get() else "BEGIN")


class Database:
    """The database file: engine, schema, sessions, and the write lock."""

    def __init__(self, path: Path, *, pool_connections: bool = False) -> None:
        self._path = path
        # Ambient state belongs to this database instance. A module-global
        # session lets repository B accidentally join repository A's
        # transaction when two control planes share one process.
        self._session: ContextVar[Session | None] = ContextVar(
            f"blitzecdn_session_{id(self)}", default=None
        )
        self._immediate: ContextVar[bool] = ContextVar(
            f"blitzecdn_begin_immediate_{id(self)}", default=False
        )
        # Kept from the sqlite3 implementation. SQLite admits one writer, and
        # serialising them here means a concurrent use case waits on a lock
        # instead of racing to `BEGIN IMMEDIATE` and failing on a busy
        # database. Reentrant because nested use cases join their caller.
        self.lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        pool_options: dict[str, Any]
        if pool_connections:
            # The API is long-lived and repeatedly reads local state. A small,
            # bounded pool avoids reconnecting for every request while keeping
            # the maximum SQLite footprint explicit.
            pool_options = {
                "poolclass": QueuePool,
                "pool_size": 5,
                "max_overflow": 5,
                "pool_timeout": 30,
            }
        else:
            # A CLI invocation is short-lived and currently builds its own
            # repository. Retaining a connection there only defers its cleanup
            # until process exit; the server opts into pooling and closes it in
            # its lifespan handler.
            pool_options = {"poolclass": NullPool}
        self.engine = create_engine(
            f"sqlite+pysqlite:///{path}",
            future=True,
            **pool_options,
        )
        _configure_sqlite(self.engine, self._immediate)
        try:
            self._initialize()
        except BaseException:
            self.engine.dispose()
            raise

    def _new_session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """The ambient session if a Unit of Work is open, else a scoped one.

        A store calls this for every operation without caring which it gets.
        Inside :meth:`transaction` all stores see the same session and one
        commit; outside it, a single-statement store call is its own
        transaction, exactly as it was.
        """
        ambient = self._session.get()
        if ambient is not None:
            yield ambient
            return
        with self.lock, self._new_session() as session, session.begin():
            yield session

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Open the Unit of Work shared by this database's stores.

        Nested use cases join the ambient transaction rather than committing a
        portion of their caller's work early — the reason this yields nothing
        and stores reach for :meth:`session` instead of being handed one.
        """
        if self._session.get() is not None:
            yield
            return
        immediate = self._immediate.set(True)
        try:
            with self.lock, self._new_session() as session, session.begin():
                # `session.begin()` is lazy: without this the transaction —
                # and so the writer reservation — would not start until the
                # first statement, which is the deferred behaviour BEGIN
                # IMMEDIATE exists to avoid. Asking for the connection is what
                # makes the boundary the boundary.
                session.connection()
                token = self._session.set(session)
                try:
                    yield
                finally:
                    self._session.reset(token)
        finally:
            self._immediate.reset(immediate)

    def require_transaction(self, operation: str) -> None:
        """Refuse an operation whose atomicity depends on an open Unit of Work.

        A compare-and-swap — read the row, compare it to what the caller
        expected, write — is only safe because :meth:`transaction` reserves the
        SQLite writer with ``BEGIN IMMEDIATE`` *before* the read, so no other
        writer can interleave between the comparison and the update. Outside
        one, :meth:`session` opens a deferred transaction instead: the read
        takes a snapshot and the write then tries to upgrade, which SQLite
        answers with a snapshot-busy error that no ``busy_timeout`` will wait
        out and that reaches the caller as something other than a conflict.

        This was a sentence in a docstring and an obligation on every caller.
        It is checked here because the failure it prevents is invisible in
        review and rare enough in testing to reach production first.
        """
        if self.current_session() is None:
            raise ValueError(
                f"{operation} must run inside a Unit of Work: its "
                "compare-and-swap is atomic only under the BEGIN IMMEDIATE "
                "that transaction() emits"
            )

    def current_session(self) -> Session | None:
        """The open Unit of Work, or ``None`` outside one.

        For asserting that a use case really did join one transaction rather
        than committing its way through several. Stores do not need it — they
        call :meth:`session` and get the right answer either way.
        """
        return self._session.get()

    def close(self) -> None:
        """Release engine-owned resources during application shutdown."""
        with self.lock:
            self.engine.dispose()

    def _initialize(self) -> None:
        """Create and validate the schema using Alembic as its sole owner."""
        # The API, worker, and installer handoff can start together after a
        # first install. The in-process RLock cannot serialize those separate
        # processes, while Alembic's empty-schema check and first CREATE TABLE
        # are not atomic. Keep a stable sidecar and take an advisory lock for
        # the complete upgrade/check boundary.
        migration_lock = self._path.with_name(f".{self._path.name}.migration.lock")
        with self.lock, migration_lock.open("a+b") as lock_file:
            migration_lock.chmod(0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            config = Config(stdout=StringIO())
            config.set_main_option(
                "script_location",
                str(Path(__file__).parents[1] / "migrations"),
            )
            config.set_main_option(
                "sqlalchemy.url",
                URL.create(
                    "sqlite+pysqlite", database=str(self._path)
                ).render_as_string(hide_password=False),
            )
            try:
                command.upgrade(config, "head")
                command.check(config)
            except (CommandError, RuntimeError) as exc:
                raise ConfigurationError(
                    f"{self._path} has an incompatible or damaged schema: {exc}. "
                    "Remove it and run `blitzecdn setup` for a clean install."
                ) from exc

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)
