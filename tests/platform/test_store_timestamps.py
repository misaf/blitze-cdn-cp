"""Every timestamp a store writes comes from the database's injected clock.

Timestamp columns carry no default, so a store that forgot to set one fails
the ``NOT NULL`` constraint instead of quietly reading the wall clock. Under a
frozen clock, every record timestamp written through every store equals it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from support.stores import Stores

from blitzecdn.capabilities.deployments.domain import DeploymentRequirementKind
from blitzecdn.capabilities.edges.domain import Edge
from blitzecdn.capabilities.releases.domain import Release, ReleaseInputs
from blitzecdn.capabilities.sites.domain import DnsRecord, Domain, Rule
from blitzecdn.core.domain.configuration import ConfigurationBundle
from blitzecdn.core.persistence.schema import DatabaseSchema
from blitzecdn.core.persistence.tables import AnsibleSettingRow, Base, UtcDateTime

# Far from any real "now", so a wall-clock read cannot match it by accident.
_FROZEN = datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)

#: The columns that record when a row was written, as opposed to instants a
#: caller hands in (``available_at``) or a later transition sets.
_RECORD_TIMESTAMPS = {"created_at", "updated_at", "requested_at"}


def _record_timestamp_columns():
    return [
        (table, column)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if column.name in _RECORD_TIMESTAMPS
    ]


def test_no_timestamp_column_falls_back_to_the_wall_clock():
    """A default would write real time under a fake clock; refuse any."""
    timestamps = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, UtcDateTime)
        and (
            column.default is not None
            or column.server_default is not None
            or column.onupdate is not None
            or column.server_onupdate is not None
        )
    ]
    assert timestamps == []


def test_record_timestamps_are_required():
    """Without a default, only ``NOT NULL`` makes a forgotten timestamp loud."""
    assert [
        f"{table.name}.{column.name}"
        for table, column in _record_timestamp_columns()
        if column.nullable
    ] == []


@pytest.fixture
def repository(tmp_path):
    path = tmp_path / "control-plane.db"
    DatabaseSchema.migrate(path)
    repository = Stores(path, clock=lambda: _FROZEN)
    yield repository
    repository.close()


def _write_through_every_store(repository: Stores) -> None:
    repository.ansible_settings.set_setting("blitzecdn_timestamp_probe", 1)
    repository.audit_log.audit("operator", "probe", "test")
    repository.idempotency_keys.reserve("operator", "probe", "fingerprint")
    repository.zones.create_domain(Domain(name="example.com"))
    repository.zones.create_record(
        DnsRecord(domain="example.com", name="cdn", value="203.0.113.10")
    )
    repository.rules.create_rule(
        Rule(
            domain="example.com",
            name="api",
            match="api.example.com",
            overrides={"cache_enabled": False},
        )
    )
    repository.edges.create_edge(Edge(name="edge-01", host="192.0.2.10"))
    repository.jobs.enqueue(kind="probe", payload={}, available_at=_FROZEN)
    repository.workflows.create("workflow-1", "deployment", "operator")
    inputs = ReleaseInputs()
    release = Release(inputs_digest=inputs.digest)
    repository.releases.save(release, inputs)
    repository.releases.save_configuration(
        release, ConfigurationBundle(files={"nginx.conf": b"events {}\n"})
    )
    repository.deployments.create_deployment(
        "operator", release_id=release.id, check_mode=False
    )
    repository.deployment_requirements.require(next(iter(DeploymentRequirementKind)))


def test_every_store_stamps_rows_from_the_injected_clock(repository):
    _write_through_every_store(repository)

    observed: dict[str, list[datetime]] = {}
    with repository.database.reader() as session:
        for table, column in _record_timestamp_columns():
            observed[f"{table.name}.{column.name}"] = list(
                session.scalars(select(column))
            )

    # Every table with a record timestamp got a row, so none went unchecked.
    assert [name for name, values in observed.items() if not values] == []
    assert {
        name: values
        for name, values in observed.items()
        if any(value != _FROZEN for value in values)
    } == {}


def test_a_row_written_without_its_timestamp_is_refused(repository):
    """The ``NOT NULL`` constraint is what replaces the old wall-clock default."""
    with pytest.raises(IntegrityError), repository.database.writer() as session:
        session.add(AnsibleSettingRow(name="forgotten", value=1))
        session.flush()
