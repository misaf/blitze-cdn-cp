"""The schema migrations, and the guard that refuses an unmigrated database.

There is one revision and no installation older than it, so there is no data
migration to verify. What still needs testing is the machinery that will carry
the *next* one: that the head revision and the constant the code compares
against agree, that a new database is stamped rather than left to replay
migrations it never needed, and that a database missing a column — or a whole
table — is refused loudly instead of half-used.

These run Alembic against a real file rather than asserting on the scripts'
text. A migration that imports cleanly and does the wrong thing is the whole
failure mode worth catching.
"""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from blitzecdn.cli.database import _alembic, current_revision
from blitzecdn.domain.dns import DnsRecord, Domain
from blitzecdn.exceptions import ConfigurationError
from blitzecdn.infrastructure.database import Repository
from blitzecdn.infrastructure.engine import SCHEMA_REVISION, Database


def _config(path) -> Config:
    config: Config = _alembic()
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    return config


def test_the_head_revision_is_the_one_the_code_expects():
    """A migration added without updating the constant would go unapplied."""
    assert ScriptDirectory.from_config(_alembic()).get_current_head() == SCHEMA_REVISION


def test_a_new_database_is_created_at_the_head_revision(tmp_path):
    """Otherwise the first real upgrade would try to replay every migration."""
    path = tmp_path / "control-plane.db"
    Database(path)
    assert current_revision(path) == SCHEMA_REVISION


def test_migrating_an_empty_file_produces_a_usable_database(tmp_path):
    """The migrations, not `create_all`, have to be able to build the schema.

    They are the only description of the schema a future upgrade applies, so a
    revision that cannot construct the database from nothing would be found out
    on the first server that needed it rather than here.
    """
    path = tmp_path / "control-plane.db"
    command.upgrade(_config(path), "head")

    repository = Repository(path)
    repository.zones.create_domain(Domain(name="example.com"))
    repository.zones.create_record(
        DnsRecord(domain="example.com", name="cdn", value="203.0.113.10", proxied=True)
    )
    assert [record.name for record in repository.zones.list_records()] == ["cdn"]
    assert current_revision(path) == SCHEMA_REVISION


def test_an_unmigrated_database_is_refused_rather_than_half_used(tmp_path):
    """`create_all` skips existing tables, so nothing else would notice.

    Simulated by dropping a column from a current database, which is what an
    installation one migration behind looks like from here.
    """
    path = tmp_path / "control-plane.db"
    Database(path)
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE edges DROP COLUMN port")
    connection.commit()
    connection.close()

    with pytest.raises(ConfigurationError) as error:
        Database(path)

    assert "older schema" in str(error.value)
    assert "blitzecdn db upgrade" in str(error.value)
    # Names a column an operator can check for themselves.
    assert "edges.port" in str(error.value)


def test_upgrading_from_the_first_revision_adds_the_rollback_guard(tmp_path):
    """The upgrade path a running installation actually takes.

    `create_all` builds the newest schema from the models, so it would pass
    even if the migration script did nothing. This walks the revisions the way
    an existing database does — stop at 0001, then step forward — which is the
    only way to find out whether 0002 carries its own change.
    """
    path = tmp_path / "control-plane.db"
    command.upgrade(_config(path), "0001")
    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(deployments)")}
    connection.close()
    assert "canonical_digest" not in columns

    command.upgrade(_config(path), "head")

    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(deployments)")}
    connection.close()
    assert "canonical_digest" in columns
    assert current_revision(path) == SCHEMA_REVISION
    # And the upgraded file is usable rather than merely shaped right.
    Repository(path).deployments.list_deployments()


def test_a_database_missing_a_whole_table_is_refused_too(tmp_path):
    """The other half of "one migration behind", and the quieter one.

    A migration that adds a table leaves an unmigrated database without it
    entirely, so there is no column for the check above to find missing.
    `create_all` is not reached — the file has tables — so nothing creates it
    either, and the controller would start cleanly and fail on `no such table`
    at whichever query reached it first, mid-deploy.
    """
    path = tmp_path / "control-plane.db"
    Database(path)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE outbox_events")
    connection.commit()
    connection.close()

    with pytest.raises(ConfigurationError) as error:
        Database(path)

    assert "older schema" in str(error.value)
    assert "blitzecdn db upgrade" in str(error.value)
    # Named as the table it is, not as a list of every column on it.
    assert "missing outbox_events." in str(error.value)
    assert "outbox_events.event_key" not in str(error.value)


def test_the_revision_reported_is_the_one_stamped(tmp_path):
    """`db current` reads the same place Alembic writes."""
    path = tmp_path / "control-plane.db"
    Database(path)
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.connect() as connection:
            stamped = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
    assert current_revision(path) == stamped
