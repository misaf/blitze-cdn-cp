"""Connection startup tolerates journal contention without retrying transactions."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import ContextVar

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from blitzecdn.core.persistence import engine as persistence
from blitzecdn.core.persistence.engine import Database


def configured_engine(path, failures):
    """Inject SQLite startup errors into otherwise real database connections."""
    connections = []

    class Cursor(sqlite3.Cursor):
        def execute(self, statement, *args):
            if statement == "PRAGMA journal_mode = WAL" and failures:
                raise failures.pop(0)
            return super().execute(statement, *args)

    class Connection(sqlite3.Connection):
        def cursor(self):
            return super().cursor(factory=Cursor)

    def connect():
        connection = sqlite3.connect(path, factory=Connection)
        connections.append(connection)
        return connection

    engine = create_engine("sqlite://", creator=connect)
    persistence._configure_sqlite(engine, ContextVar("immediate", default=False))
    return engine, connections


def sqlite_error(code):
    error = sqlite3.OperationalError("injected SQLite error")
    error.sqlite_errorcode = code
    return error


def test_startup_retries_busy_and_restores_connection_settings(tmp_path):
    engine, connections = configured_engine(
        tmp_path / "startup.db", [sqlite_error(sqlite3.SQLITE_BUSY)]
    )
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30_000
        assert len(connections) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("code", [sqlite3.SQLITE_READONLY, sqlite3.SQLITE_LOCKED])
def test_other_startup_errors_are_not_retried_and_close_connection(tmp_path, code):
    failures = [sqlite_error(code), sqlite_error(sqlite3.SQLITE_BUSY)]
    engine, connections = configured_engine(tmp_path / "startup.db", failures)
    try:
        with pytest.raises(OperationalError) as caught:
            engine.connect()
        assert caught.value.orig.sqlite_errorcode == code
        assert len(failures) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connections[0].execute("SELECT 1")
    finally:
        engine.dispose()


def test_startup_contention_has_a_deadline_and_closes_failed_connection(
    tmp_path, monkeypatch
):
    clock = [0.0]
    monkeypatch.setattr(persistence, "monotonic", lambda: clock[0])

    def advance(_seconds):
        clock[0] += 30

    monkeypatch.setattr(persistence, "sleep", advance)
    engine, connections = configured_engine(
        tmp_path / "startup.db", [sqlite_error(sqlite3.SQLITE_BUSY) for _ in range(3)]
    )
    try:
        with pytest.raises(OperationalError) as caught:
            engine.connect()
        assert caught.value.orig.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert clock[0] == 30
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connections[0].execute("SELECT 1")
    finally:
        engine.dispose()


@pytest.mark.parametrize("pooled", [False, True])
def test_simultaneous_first_connections_preserve_all_writes(settings, pooled):
    databases = [
        Database(settings.database_path, pool_connections=pooled) for _ in range(6)
    ]
    try:
        with closing(sqlite3.connect(settings.database_path)) as setup:
            setup.execute("CREATE TABLE startup_test (value INTEGER NOT NULL)")
            setup.execute("INSERT INTO startup_test VALUES (0)")
            setup.commit()
        for _ in range(10):
            # Repeat the DELETE -> WAL race, rather than warming up connections.
            for database in databases:
                database.close()
            with closing(sqlite3.connect(settings.database_path)) as setup:
                assert setup.execute("PRAGMA journal_mode = DELETE").fetchone() == (
                    "delete",
                )
            barrier = threading.Barrier(len(databases))

            def increment(database, start=barrier):
                start.wait(timeout=10)
                with database.transaction(), database.session() as session:
                    session.connection().exec_driver_sql(
                        "UPDATE startup_test SET value = value + 1"
                    )

            with ThreadPoolExecutor(max_workers=len(databases)) as executor:
                list(executor.map(increment, databases))
        with databases[0].session() as session:
            assert (
                session.connection()
                .exec_driver_sql("SELECT value FROM startup_test")
                .scalar()
                == 60
            )
    finally:
        for database in databases:
            database.close()
