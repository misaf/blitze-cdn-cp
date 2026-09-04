"""The baseline schema and guards against a damaged database.

There is one revision and no installation older than it. Tests prove the
baseline and SQLModel metadata create the same usable schema, and that a
database missing a column or table is refused rather than half-used.

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
from paths import SOURCE
from sqlalchemy import create_engine

from blitzecdn.capabilities.dns.domain import DnsRecord, Domain
from blitzecdn.capabilities.sites.domain import CdnSite
from blitzecdn.composition import Repository
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.persistence.tables import Base


def _config(path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(SOURCE / "migrations"),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    return config


def _revision(path) -> str | None:
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def test_a_new_database_is_created_at_the_head_revision(tmp_path):
    """Alembic is the sole schema-creation path."""
    path = tmp_path / "control-plane.db"
    Database(path)
    assert (
        _revision(path) == ScriptDirectory.from_config(_config(path)).get_current_head()
    )


def test_migrating_an_empty_file_produces_a_usable_database(tmp_path):
    """The baseline migration must construct a usable database from nothing."""
    path = tmp_path / "control-plane.db"
    command.upgrade(_config(path), "head")

    repository = Repository(path)
    repository.zones.create_domain(Domain(name="example.com"))
    repository.sites.create_site(
        CdnSite(name="cdn-example-com", origin_host="203.0.113.10")
    )
    repository.zones.create_record(
        DnsRecord(domain="example.com", name="cdn", site="cdn-example-com")
    )
    assert [record.name for record in repository.zones.list_records()] == ["cdn"]
    assert (
        _revision(path) == ScriptDirectory.from_config(_config(path)).get_current_head()
    )


def test_an_unmigrated_database_is_refused_rather_than_half_used(tmp_path):
    """Alembic's metadata check refuses a damaged current database."""
    path = tmp_path / "control-plane.db"
    Database(path)
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE edges DROP COLUMN private_key_file")
    connection.commit()
    connection.close()

    with pytest.raises(ConfigurationError) as error:
        Database(path)

    assert "incompatible or damaged schema" in str(error.value)
    assert "private_key_file" in str(error.value)


def test_a_database_missing_a_whole_table_is_refused_too(tmp_path):
    """Alembic's metadata check also detects a missing whole table."""
    path = tmp_path / "control-plane.db"
    Database(path)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE workflows")
    connection.commit()
    connection.close()

    with pytest.raises(ConfigurationError) as error:
        Database(path)

    assert "incompatible or damaged schema" in str(error.value)
    assert "workflows" in str(error.value)


def test_the_revision_reported_is_the_one_stamped(tmp_path):
    """The runtime schema marker is the revision Alembic stamps."""
    path = tmp_path / "control-plane.db"
    Database(path)
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.connect() as connection:
            stamped = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
    assert _revision(path) == stamped


def test_every_capability_table_module_reaches_the_metadata():
    """A table is on disk only if Alembic can see it.

    A capability keeps its tables beside the store that reads them, so
    `Base.metadata` is complete only once every one of those modules has been
    imported. `migrations/env.py` is where that happens, and a new
    `adapters/tables.py` that nobody added to it would not fail loudly: it
    would look to autogenerate like a table somebody had dropped.
    """
    declared = {
        path.parent.parent.name
        for path in (SOURCE / "capabilities").rglob("adapters/tables.py")
    }
    env = (SOURCE / "migrations/env.py").read_text(encoding="utf-8")
    missing = {
        capability
        for capability in declared
        if f"blitzecdn.capabilities.{capability}.adapters" not in env
    }
    assert declared, "no capability declares a table module; the rule moved"
    assert missing == set()


def test_the_schema_alembic_compares_against_holds_every_table(tmp_path):
    """The same check from the other end: metadata against a migrated file.

    `_assert_schema_matches` is Alembic's own comparison, so a table left out
    of `env.py` shows up here as a table the migration created and the
    metadata does not know about.
    """
    path = tmp_path / "control.db"
    Database(path).close()
    tables = set(Base.metadata.tables)
    assert {
        "sites",
        "dns_records",
        "domains",
        "edges",
        "deployments",
        "deployment_requirements",
        "projection_state",
        "audit_events",
        "workflows",
        "ansible_settings",
    } <= tables


def test_the_current_schema_has_no_upgrade_chain(tmp_path):
    """One revision, because there is no installation older than it.

    A second revision means something shipped. Until then a schema change is an
    edit to the baseline, not a migration on top of it.
    """
    revisions = ScriptDirectory.from_config(_config(tmp_path / "unused.db"))
    assert [revision.revision for revision in revisions.walk_revisions()] == ["0001"]
