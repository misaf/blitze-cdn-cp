"""The baseline schema and guards against a damaged database.

The baseline and SQLAlchemy metadata create the same schema, and a database
missing a column or table is refused. Alembic runs against a real file.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from support.paths import SOURCE

from blitzecdn.capabilities.dns.domain import DnsRecord, Domain
from blitzecdn.composition import Repository
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.persistence.schema import DatabaseSchema
from blitzecdn.core.persistence.tables import Base


def _config(path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(SOURCE / "assets/migrations"),
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
    DatabaseSchema.migrate(path)
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
        DnsRecord(domain="example.com", name="cdn", value="203.0.113.10")
    )
    assert [record.name for record in repository.zones.list_records()] == ["cdn"]
    assert (
        _revision(path) == ScriptDirectory.from_config(_config(path)).get_current_head()
    )


def test_an_unmigrated_database_is_refused_rather_than_half_used(tmp_path):
    """Alembic's metadata check refuses a damaged current database."""
    path = tmp_path / "control-plane.db"
    DatabaseSchema.migrate(path)
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
    DatabaseSchema.migrate(path)
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
    DatabaseSchema.migrate(path)
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.connect() as connection:
            stamped = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
    assert _revision(path) == stamped


def test_every_capability_table_module_reaches_the_metadata():
    """A table is on disk only if Alembic can see it.

    Tables live beside their stores, so `Base.metadata` is complete only once
    `migrations/env.py` imports every table module; a missing one would look to
    autogenerate like a dropped table.
    """
    declared = {
        path.parent.parent.name
        for path in (SOURCE / "capabilities").rglob("adapters/tables.py")
    }
    env = (SOURCE / "assets/migrations/env.py").read_text(encoding="utf-8")
    missing = {
        capability
        for capability in declared
        if f"blitzecdn.capabilities.{capability}.adapters" not in env
    }
    assert declared, "no capability declares a table module; the rule moved"
    assert missing == set()


def test_the_schema_alembic_compares_against_holds_every_table(tmp_path):
    """The same check from the other end: metadata against a migrated file.

    `Database()` already compares metadata with the file; this pins the expected
    table set, so dropping one from both halves still fails.
    """
    path = tmp_path / "control.db"
    DatabaseSchema.migrate(path)
    tables = set(Base.metadata.tables)
    assert {
        "zone_rules",
        "dns_records",
        "domains",
        "edges",
        "deployments",
        "deployment_requirements",
        "audit_events",
        "workflows",
        "ansible_settings",
    } <= tables


#: The initial revisions in upgrade order, each named after the capability
#: whose tables it creates.
INITIAL = (
    "0001_core",
    "0002_jobs",
    "0003_releases",
    "0004_deployments",
    "0005_dns",
    "0006_edges",
    "0007_workflows",
    "0008_traffic",
)


def _tables(path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        } - {"alembic_version"}


def _owner(module: str) -> str:
    """The capability a table module belongs to; core owns the shared tables."""
    if module == "blitzecdn.core.persistence.tables":
        return "core"
    return module.removeprefix("blitzecdn.capabilities.").removesuffix(
        ".adapters.tables"
    )


def test_initial_capability_revisions_form_one_chain(tmp_path):
    revisions = ScriptDirectory.from_config(_config(tmp_path / "unused.db"))
    chain = [revision.revision for revision in revisions.walk_revisions()]
    assert chain[-len(INITIAL) :] == list(reversed(INITIAL))


def test_each_initial_revision_creates_exactly_its_capabilitys_tables(tmp_path):
    """The capability grouping is enforced, not just a naming convention."""
    owners = {
        mapper.local_table.name: _owner(mapper.class_.__module__)
        for mapper in Base.registry.mappers
    }
    path = tmp_path / "control.db"
    config = _config(path)
    before: set[str] = set()
    for revision in INITIAL:
        command.upgrade(config, revision)
        after = _tables(path)
        capability = revision.split("_", 1)[1]
        assert after - before == {
            table for table, owner in owners.items() if owner == capability
        }, revision
        before = after


def test_initial_revisions_upgrade_and_downgrade_individually(tmp_path):
    path = tmp_path / "control.db"
    config = _config(path)
    for revision in INITIAL:
        command.upgrade(config, revision)
        assert _revision(path) == revision
    DatabaseSchema.validate(path)
    for revision in reversed(INITIAL[:-1]):
        command.downgrade(config, revision)
        assert _revision(path) == revision
    command.downgrade(config, "base")
    assert _revision(path) is None
    assert _tables(path) == set()


def test_a_database_from_the_retired_single_baseline_is_refused(tmp_path):
    """Before the split, revision "0001" held the whole schema.

    No current revision reuses that id, so such a file or backup is refused as
    unknown rather than half-upgraded on top of tables that already exist.
    """
    assert not DatabaseSchema.known("0001")
    legacy = tmp_path / "legacy.db"
    DatabaseSchema.migrate(legacy)
    with closing(sqlite3.connect(legacy)) as connection:
        connection.execute("UPDATE alembic_version SET version_num = '0001'")
        connection.commit()
    before = legacy.read_bytes()
    with pytest.raises(ConfigurationError, match="unknown schema revision '0001'"):
        DatabaseSchema.migrate(legacy)
    assert legacy.read_bytes() == before


def test_repository_startup_does_not_create_a_missing_database_or_directory(tmp_path):
    path = tmp_path / "missing" / "controller.db"
    with pytest.raises(ConfigurationError, match="blitzecdn setup"):
        Repository(path)
    assert not path.parent.exists()


@pytest.mark.parametrize("revision", [None, "unknown-revision"])
def test_repository_refuses_an_unprepared_schema_without_modifying_it(
    tmp_path, revision
):
    path = tmp_path / "controller.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE preserved (value TEXT)")
        connection.execute("INSERT INTO preserved VALUES ('operator data')")
        if revision is not None:
            connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
            connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        connection.commit()
    before = path.read_bytes()
    with pytest.raises(ConfigurationError, match="blitzecdn migrate"):
        Repository(path)
    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]


def test_validated_startup_is_read_only_and_never_calls_upgrade(tmp_path, monkeypatch):
    path = tmp_path / "controller.db"
    DatabaseSchema.migrate(path)
    path.with_name(f".{path.name}.migration.lock").unlink()
    before = path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("repository startup attempted schema migration")

    monkeypatch.setattr(command, "upgrade", forbidden)
    Repository(path).close()
    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]


def test_migration_and_inspection_support_uri_and_interpolation_characters(tmp_path):
    path = tmp_path / "controller %?# database.db"
    DatabaseSchema.migrate(path)
    DatabaseSchema.migrate(path)
    assert (
        DatabaseSchema.of(path)
        == ScriptDirectory.from_config(
            _config(tmp_path / "unused.db")
        ).get_current_head()
    )
    repository = Repository(path)
    repository.zones.create_domain(Domain(name="example.com"))
    assert repository.zones.get_domain("example.com").name == "example.com"
    repository.close()


def test_concurrent_migration_processes_initialize_one_database(tmp_path):
    import os
    import subprocess
    import sys

    path = tmp_path / "controller.db"
    code = (
        "from pathlib import Path; "
        "from blitzecdn.core.persistence.schema import DatabaseSchema; "
        "import sys; DatabaseSchema.migrate(Path(sys.argv[1]))"
    )
    processes = [
        subprocess.Popen(  # noqa: S603 - fixed local interpreter and test database
            [sys.executable, "-c", code, str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )
        for _ in range(3)
    ]
    for process in processes:
        _, error = process.communicate(timeout=30)
        assert process.returncode == 0, error.decode()
    Repository(path).close()


def test_parallel_repository_startup_validates_one_prepared_schema(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "controller.db"
    DatabaseSchema.migrate(path)

    def open_and_close(_):
        repository = Repository(path)
        try:
            assert repository.zones.list_domains() == []
        finally:
            repository.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(open_and_close, range(12)))


def test_runtime_connection_cannot_recreate_a_removed_database(tmp_path):
    path = tmp_path / "controller.db"
    DatabaseSchema.migrate(path)
    repository = Repository(path)
    path.unlink()
    from sqlalchemy.exc import OperationalError

    try:
        with pytest.raises(OperationalError, match="unable to open database"):
            repository.zones.list_domains()
        assert not path.exists()
    finally:
        repository.close()
