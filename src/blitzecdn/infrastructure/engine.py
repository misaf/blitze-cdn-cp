"""The engine, the session, and the transaction boundary.

Owns the three things that were hand-rolled around ``sqlite3`` before: the
connection, the process-wide write lock, and the Unit of Work every store
joins. The behaviour is deliberately the same; only the machinery underneath
changed.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from blitzecdn.exceptions import ConfigurationError
from blitzecdn.infrastructure.models import Base

#: The migration this code expects the database to be at. Must equal the head
#: revision in ``migrations/versions``; ``tests/test_migrations.py`` checks it.
SCHEMA_REVISION = "0001"

#: The ambient Unit of Work. A ContextVar rather than ``threading.local`` so a
#: transaction is scoped to the logical operation: queued deployments finish on
#: a worker thread, and FastAPI may run a sync endpoint on a threadpool thread
#: that is not the one that started the request.
_SESSION: ContextVar[Session | None] = ContextVar("blitzecdn_session", default=None)

#: Whether the transaction about to begin should reserve the writer.
#:
#: Only a Unit of Work does. A standalone read must not: escalating every
#: ``SELECT`` to a write lock would serialise readers against each other, which
#: WAL exists to avoid. Consulted by the ``begin`` handler below, which cannot
#: otherwise tell the two apart.
_IMMEDIATE: ContextVar[bool] = ContextVar("blitzecdn_begin_immediate", default=False)


def _configure_sqlite(engine: Engine) -> None:
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
        connection.exec_driver_sql("BEGIN IMMEDIATE" if _IMMEDIATE.get() else "BEGIN")


class Database:
    """The database file: engine, schema, sessions, and the write lock."""

    def __init__(self, path: Path, *, pool_connections: bool = False) -> None:
        self._path = path
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
        _configure_sqlite(self.engine)
        self._sessions = sessionmaker(
            bind=self.engine, expire_on_commit=False, future=True
        )
        self._initialize()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """The ambient session if a Unit of Work is open, else a scoped one.

        A store calls this for every operation without caring which it gets.
        Inside :meth:`transaction` all stores see the same session and one
        commit; outside it, a single-statement store call is its own
        transaction, exactly as it was.
        """
        ambient = _SESSION.get()
        if ambient is not None:
            yield ambient
            return
        with self.lock, self._sessions() as session, session.begin():
            yield session

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Open the Unit of Work shared by this database's stores.

        Nested use cases join the ambient transaction rather than committing a
        portion of their caller's work early — the reason this yields nothing
        and stores reach for :meth:`session` instead of being handed one.
        """
        if _SESSION.get() is not None:
            yield
            return
        immediate = _IMMEDIATE.set(True)
        try:
            with self.lock, self._sessions() as session, session.begin():
                # `session.begin()` is lazy: without this the transaction —
                # and so the writer reservation — would not start until the
                # first statement, which is the deferred behaviour BEGIN
                # IMMEDIATE exists to avoid. Asking for the connection is what
                # makes the boundary the boundary.
                session.connection()
                token = _SESSION.set(session)
                try:
                    yield
                finally:
                    _SESSION.reset(token)
        finally:
            _IMMEDIATE.reset(immediate)

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
        if _SESSION.get() is None:
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
        return _SESSION.get()

    def close(self) -> None:
        """Release engine-owned resources during application shutdown."""
        with self.lock:
            self.engine.dispose()

    def _initialize(self) -> None:
        """Create the schema on a fresh database, or refuse a stale one.

        Alembic owns *changes*; this owns the empty-file case, so a new install
        is usable without a migration run. It is stamped at the head revision
        on the way, because a database created from the current models has, by
        definition, every migration already applied — leaving it unstamped
        would make the first real upgrade try to replay them.

        An existing database missing anything the models declare is refused
        outright. ``create_all`` silently skips tables that already exist, so
        without this check a controller whose package has moved ahead of its
        database would run against the older shape and fail one confusing
        query at a time, mid-deploy, rather than at startup.
        """
        with self.lock:
            inspector = inspect(self.engine)
            tables = set(inspector.get_table_names())
            if not tables - {"alembic_version"}:
                Base.metadata.create_all(self.engine)
                self._stamp(SCHEMA_REVISION)
                return
            self._require_current_schema(inspector, tables)

    def _require_current_schema(self, inspector: Inspector, tables: set[str]) -> None:
        """Refuse a database that does not carry everything the models declare.

        Both halves of "missing" count. A database one migration behind may be
        short a column on a table it already has, or short the whole table that
        migration introduced — and only the first was checked here, so a
        controller whose package had gained a table would start cleanly and then
        fail on `no such table` at whichever query reached it first, mid-deploy.
        An absent table is named on its own rather than through each of its
        columns, so the message stays the size of the problem.
        """
        missing: list[str] = []
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                missing.append(table.name)
                continue
            present = {column["name"] for column in inspector.get_columns(table.name)}
            missing.extend(
                f"{table.name}.{column.name}"
                for column in table.columns
                if column.name not in present
            )
        if missing:
            raise ConfigurationError(
                f"{self._path} is on an older schema and is missing "
                f"{', '.join(sorted(missing))}. Run `blitzecdn db upgrade` to "
                "migrate it. "
                "Take a copy of the file first; the migration rewrites tables."
            )

    def _stamp(self, revision: str) -> None:
        """Record the schema revision without running any migration."""
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY)"
            )
            connection.exec_driver_sql("DELETE FROM alembic_version")
            connection.exec_driver_sql(
                "INSERT INTO alembic_version (version_num) VALUES (?)", (revision,)
            )

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)
