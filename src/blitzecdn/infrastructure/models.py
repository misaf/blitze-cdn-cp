"""The physical schema, as SQLAlchemy declarative models.

This module is the *only* description of what is on disk, and Alembic
generates migrations by comparing against it. Nothing else may create or alter
a table: a schema built by anything but a migration is a schema no upgrade can
reason about.

What is a column, and what is JSON
----------------------------------
A field earns a column when something queries it: identity, foreign keys,
lifecycle status, timestamps, and anything a ``WHERE`` or ``ORDER BY`` names
today. Nested value objects that no query reaches — a site's cache and firewall
policy, the ``AnsibleRun`` a deployment produced, a workflow's steps, an outbox
payload — stay whole in a JSON column.

That line is deliberate rather than lazy. Normalising a site policy into tables
would give the pydantic models in :mod:`blitzecdn.domain` a rival definition of
the same invariants, with nothing to keep the two honest. Storing everything as
one opaque blob instead would bury facts SQL needs to see — an edge's host and
port, a record's TTL. A column per queryable fact and JSON for the policy is
what keeps exactly one source of truth for each.

These models hold no invariants of their own. Validation belongs to the domain
models, which every store still round-trips through.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware UTC instant, stored as the ISO-8601 text it always was.

    Not :class:`sqlalchemy.DateTime`. On SQLite that type stores
    ``YYYY-MM-DD HH:MM:SS.ffffff`` and drops the offset, which would do two
    unrelated kinds of damage here: every ``datetime.now(UTC)`` this control
    plane records would come back naive, and the on-disk text would stop being
    the ISO-8601 the Ansible inventory plugin already parses out of this file
    with the standard library.

    Storing the offset explicitly also keeps the column meaningful on a
    dialect with real timestamp support later — the values are unambiguous
    instants rather than wall-clock strings whose zone you have to know.
    """

    impl = String
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("refusing to store a naive datetime; attach UTC")
        return value.astimezone(UTC).isoformat()

    def process_result_value(
        self, value: str | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        # Everything this writes carries an offset. A row without one was
        # edited by hand, and UTC is the only thing it can have meant — worth
        # reading rather than raising, since the alternative is a controller
        # that will not start over one hand-fixed timestamp.
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for every table in the control-plane database."""

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: UtcDateTime,
        dict[str, Any]: JSON,
        list[Any]: JSON,
    }


class DomainRow(Base):
    """A delegated zone. Holds no records; they are keyed by name below."""

    __tablename__ = "domains"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)

    records: Mapped[list[DnsRecordRow]] = relationship(
        back_populates="domain_row",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="(DnsRecordRow.name, DnsRecordRow.type)",
    )


class DnsRecordRow(Base):
    """One record, plus the CDN policy that applies when it is proxied.

    ``value``, ``ttl`` and ``proxied`` are columns because they are the answer
    DNS gives and the switch that decides whether a site exists at all. The
    inherited :class:`~blitzecdn.domain.sites.SitePolicy` fields — cache rules,
    firewall, headers, certificate mode — stay in ``policy``: nothing queries
    inside them, and they are validated by the domain model on the way out.
    """

    __tablename__ = "dns_records"

    # ON DELETE CASCADE is load-bearing rather than convenience: a record
    # outliving its domain would keep deriving a virtual host for a zone we no
    # longer serve. `PRAGMA foreign_keys` is enabled per connection in
    # database.py, without which SQLite ignores this entirely.
    domain: Mapped[str] = mapped_column(
        ForeignKey("domains.name", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str]
    ttl: Mapped[int]
    proxied: Mapped[bool]
    policy: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)

    domain_row: Mapped[DomainRow] = relationship(back_populates="records")


class SiteRow(Base):
    """The derived virtual hosts.

    The one table that is a projection rather than a source of truth: it is
    rebuilt wholesale from records, so an edit made directly here survives only
    until the next record change. ``policy`` stays whole for that reason — it
    is never queried into, only regenerated.
    """

    __tablename__ = "sites"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    #: The virtual host names nginx answers on, and where it fetches from.
    #: Columns because "which site serves this hostname" is a question worth
    #: asking of the database rather than of every decoded policy in turn.
    server_names: Mapped[list[Any]] = mapped_column(default=list)
    origin_host: Mapped[str]
    policy: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class EdgeRow(Base):
    """The fleet, and the table the Ansible inventory plugin reads.

    This table is a published contract, not private storage. The plugin in
    ``ansible/plugins/inventory/`` opens the file read-only at the start of
    every run, possibly under a different interpreter with no ``blitzecdn`` on
    its path, so it reads these columns directly and has no model to validate
    against. That is why this table alone carries ``schema_version``: the
    plugin checks it first and refuses a version it was not written for,
    rather than publishing a fleet it half understood.

    Changing a column here means changing the plugin and bumping
    :data:`~blitzecdn.domain.edges.EDGE_SCHEMA_VERSION` in the same commit.
    ``tests/test_inventory.py`` fails if the two drift.
    """

    __tablename__ = "edges"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int]
    host: Mapped[str]
    user: Mapped[str]
    port: Mapped[int]
    private_key_file: Mapped[str | None]
    #: Lists rather than a child table: they are small, always read whole with
    #: the edge, and never joined or filtered on.
    public_addresses: Mapped[list[Any]] = mapped_column(default=list)
    ssh_sources: Mapped[list[Any]] = mapped_column(default=list)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class AnsibleSettingRow(Base):
    """Non-secret, fleet-wide Ansible policy.

    ``value`` is JSON because the whole point is that an operator sets an
    arbitrary Ansible variable — a bool, an int, a list of CIDRs. A column per
    setting would mean a migration per setting.
    """

    __tablename__ = "ansible_settings"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class DeploymentRow(Base):
    """Convergence history.

    ``result`` is the JSON of one :class:`~blitzecdn.domain.runs.AnsibleRun`:
    per-host counters, the tasks that changed, the tasks that failed. Raw
    stdout and stderr are deliberately not here — they are the largest thing a
    run produces and every reader would have to re-parse them to learn
    anything. They live in a log file the result names, outside the database.
    """

    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, index=True)
    operator: Mapped[str]
    check_mode: Mapped[bool]
    rollback_of: Mapped[str | None] = mapped_column(ForeignKey("deployments.id"))
    host_limit: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    #: The desired state this deployment converged, and can roll back to.
    #: Opaque here on purpose — its shape is versioned by
    #: :mod:`blitzecdn.domain.snapshots`, not by this schema.
    snapshot: Mapped[str]
    #: For a rollback: a digest of the canonical desired state as it stood when
    #: this rollback was queued. Adoption compares it against canonical state
    #: again and refuses if it moved, because ``replace_all_records`` restores
    #: wholesale and would otherwise delete a record written while the rollback
    #: was converging — silently, with nothing left to say it existed.
    #:
    #: A digest rather than a second snapshot: these rows already carry a full
    #: copy of desired state, and all this needs to answer is "the same or not".
    #: ``NULL`` on an ordinary deployment, which adopts nothing.
    canonical_digest: Mapped[str | None]


class AuditEventRow(Base):
    """The append-only record of who changed what."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    operator: Mapped[str]
    action: Mapped[str]
    resource_type: Mapped[str]
    resource_id: Mapped[str | None]
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


class WorkflowRow(Base):
    """Crash-visible progress for work that crosses out of this transaction."""

    __tablename__ = "workflows"
    __table_args__ = (Index("workflows_status_idx", "status", "updated_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str]
    resource_id: Mapped[str | None]
    status: Mapped[str]
    operator: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    #: An append-only list of checkpoints. Ordered, read whole, never queried
    #: into — a child table would buy nothing but joins.
    steps: Mapped[list[Any]] = mapped_column(default=list)
    error: Mapped[str | None]


class OutboxEventRow(Base):
    """A committed integration event awaiting idempotent delivery."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        # The uniqueness that makes redelivery safe: the observer writes with
        # INSERT OR IGNORE keyed on this, inside the transaction that published
        # the event.
        UniqueConstraint("event_key", name="outbox_events_event_key_key"),
        Index("outbox_pending_idx", "delivered_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic: Mapped[str]
    event_key: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    delivered_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None]


class ProjectionStateRow(Base):
    """What revision of the source a derived table was last built from."""

    __tablename__ = "projection_state"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    source_revision: Mapped[str]
    projected_at: Mapped[datetime] = mapped_column(default=utcnow)
