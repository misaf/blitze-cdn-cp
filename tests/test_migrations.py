"""The baseline schema and guards against a damaged database.

There is one revision and no installation older than it. Tests prove the
baseline and SQLModel metadata create the same usable schema, and that a
database missing a column or table is refused rather than half-used.

These run Alembic against a real file rather than asserting on the scripts'
text. A migration that imports cleanly and does the wrong thing is the whole
failure mode worth catching.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from blitzecdn.domain.dns import DnsRecord, Domain
from blitzecdn.exceptions import ConfigurationError
from blitzecdn.infrastructure.database import Repository
from blitzecdn.infrastructure.engine import Database


def _config(path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[1] / "src/blitzecdn/migrations"),
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
    repository.zones.create_record(
        DnsRecord(domain="example.com", name="cdn", value="203.0.113.10", proxied=True)
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


def test_ssl_mode_migration_normalizes_record_and_site_policies(tmp_path):
    path = tmp_path / "legacy.db"
    command.upgrade(_config(path), "0001")
    connection = sqlite3.connect(path)
    timestamp = "2026-08-24T00:00:00+00:00"
    connection.execute(
        "INSERT INTO domains (name, updated_at) VALUES (?, ?)",
        ("example.com", timestamp),
    )
    policies = {
        "disabled-https": {"origin_scheme": "https", "certificate_mode": "disabled"},
        "active-http": {"origin_scheme": "http", "certificate_mode": "existing"},
        "active-https": {"origin_scheme": "https", "certificate_mode": "existing"},
    }
    for name, policy in policies.items():
        connection.execute(
            """INSERT INTO dns_records
               (domain, name, type, value, ttl, proxied, policy, updated_at)
               VALUES (?, ?, 'A', '192.0.2.1', 300, 1, ?, ?)""",
            ("example.com", name, json.dumps(policy), timestamp),
        )
    connection.execute(
        """INSERT INTO sites
           (name, server_names, origin_host, policy, updated_at)
           VALUES ('active-http', '[\"active.example.com\"]', '192.0.2.1', ?, ?)""",
        (json.dumps(policies["active-http"]), timestamp),
    )
    connection.commit()
    connection.close()

    command.upgrade(_config(path), "head")

    connection = sqlite3.connect(path)
    rows = dict(connection.execute("SELECT name, policy FROM dns_records"))
    site_policy = connection.execute(
        "SELECT policy FROM sites WHERE name = 'active-http'"
    ).fetchone()[0]
    connection.close()
    assert json.loads(rows["disabled-https"])["ssl_mode"] == "off"
    assert json.loads(rows["active-http"])["ssl_mode"] == "flexible"
    assert json.loads(rows["active-https"])["ssl_mode"] == "full_strict"
    assert json.loads(site_policy)["ssl_mode"] == "flexible"
    assert all("origin_scheme" not in json.loads(policy) for policy in rows.values())

    command.downgrade(_config(path), "0001")
    connection = sqlite3.connect(path)
    downgraded = dict(connection.execute("SELECT name, policy FROM dns_records"))
    connection.close()
    assert json.loads(downgraded["active-http"])["origin_scheme"] == "http"
    assert json.loads(downgraded["active-https"])["origin_scheme"] == "https"
