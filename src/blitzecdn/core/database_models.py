"""The physical schema, as SQLModel table models.

This module is the *only* description of what is on disk, and Alembic
generates migrations by comparing against it. Nothing else may create or alter
a table: a schema built by anything but a migration is a schema no upgrade can
reason about.

What is a column, and what is JSON
----------------------------------
A field earns a column when something queries it: identity, foreign keys,
    lifecycle status, timestamps, and anything a ``WHERE`` or ``ORDER BY`` names
today. Nested value objects that no query reaches — a site's cache and firewall
policy, the ``AnsibleRun`` a deployment produced, or a workflow's steps — stay
whole in a JSON column.

That line is deliberate rather than lazy. Normalising a site policy into tables
would give the capability domain models a rival definition of
the same invariants, with nothing to keep the two honest. Storing everything as
one opaque blob instead would bury facts SQL needs to see — an edge's host and
port, a record's TTL. A column per queryable fact and JSON for the policy is
what keeps exactly one source of truth for each.

These models hold no invariants of their own. Validation belongs to the domain
models, which every store still round-trips through.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    TypeDecorator,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.engine.interfaces import Dialect
from sqlmodel import Field, SQLModel


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
        if parsed.tzinfo is None:
            raise ValueError("stored timestamps must include a UTC offset")
        return parsed


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(SQLModel):
    """Declarative base for every table in the control-plane database."""

    __abstract__ = True


class DomainRow(Base, table=True):
    """A delegated zone. Holds no records; they are keyed by name below."""

    __tablename__ = "domains"
    __table_args__ = (
        CheckConstraint("length(name) > 0", name="domains_name_nonempty_check"),
    )

    name: str = Field(sa_column=Column(String, primary_key=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class DnsRecordRow(Base, table=True):
    """One record: an address of its own, or a route to a site.

    There is no ``policy`` column any more and no ``proxied`` flag. Both
    existed because a record used to *be* a site; a site is its own row now, so
    what is left here is the answer DNS gives (``value``, ``ttl``) and the site
    that answers instead (``site``).

    The check constraint is the database's copy of the domain rule: exactly one
    of ``value`` and ``site``. It is written down twice deliberately — records
    also arrive from a restored backup and from a rollback's wholesale rewrite,
    neither of which goes through the record editor.
    """

    __tablename__ = "dns_records"
    __table_args__ = (
        CheckConstraint("ttl BETWEEN 1 AND 604800", name="dns_records_ttl_check"),
        CheckConstraint("type IN ('A', 'AAAA')", name="dns_records_type_check"),
        CheckConstraint("length(name) > 0", name="dns_records_name_nonempty_check"),
        CheckConstraint(
            "(value IS NULL) <> (site IS NULL)", name="dns_records_target_check"
        ),
        CheckConstraint(
            "value IS NULL OR length(value) > 0",
            name="dns_records_value_nonempty_check",
        ),
    )

    # ON DELETE CASCADE is load-bearing rather than convenience: a record
    # outliving its domain would keep deriving a virtual host for a zone we no
    # longer serve. `PRAGMA foreign_keys` is enabled per connection in
    # database.py, without which SQLite ignores this entirely.
    domain: str = Field(
        sa_column=Column(
            String, ForeignKey("domains.name", ondelete="CASCADE"), primary_key=True
        )
    )
    name: str = Field(sa_column=Column(String, primary_key=True))
    type: str = Field(sa_column=Column(String, primary_key=True))
    value: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    ttl: int
    # RESTRICT rather than CASCADE or SET NULL: deleting a site that hostnames
    # still route to is a mistake worth refusing, not one to resolve by
    # guessing. `SiteService.delete_site` names the records first; this is the
    # backstop for the paths that do not go through it.
    site: str | None = Field(
        default=None,
        sa_column=Column(
            String,
            ForeignKey("sites.name", ondelete="RESTRICT"),
            nullable=True,
            index=True,
        ),
    )
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class SiteRow(Base, table=True):
    """The virtual hosts, and the source of truth for how each one is served.

    ``policy`` stays whole because nothing queries inside it: it is written and
    read back as one document and validated by the domain model on the way out.

    ``server_names`` is the one column with a writer outside this capability. It is
    maintained by `dns` from the records routed to the site, so it is a
    projection of a *relationship* rather than of the site — which is why the
    row it lives on is canonical and this column is not.
    """

    __tablename__ = "sites"

    name: str = Field(sa_column=Column(String, primary_key=True))
    #: The virtual host names nginx answers on. A column because "which site
    #: serves this hostname" is a question worth asking of the database rather
    #: than of every decoded policy in turn.
    server_names: list[Any] = Field(default_factory=list, sa_type=JSON)
    origin_host: str
    policy: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class EdgeRow(Base, table=True):
    """The fleet, and the table the Ansible inventory plugin reads.

    This table is a published contract, not private storage. The plugin in
    ``ansible/plugins/inventory/`` opens the file read-only at the start of
    every run, possibly under a different interpreter with no ``blitzecdn`` on
    its path, so it reads these columns directly and has no model to validate
    against. Its explicit SELECT is the schema contract, so changes here and
    in that query must land together.
    """

    __tablename__ = "edges"
    __table_args__ = (
        CheckConstraint("port BETWEEN 1 AND 65535", name="edges_port_check"),
        CheckConstraint("length(host) > 0", name="edges_host_nonempty_check"),
        CheckConstraint("length(user) > 0", name="edges_user_nonempty_check"),
    )

    name: str = Field(sa_column=Column(String, primary_key=True))
    host: str
    user: str
    port: int
    private_key_file: str | None = None
    #: Lists rather than a child table: they are small, always read whole with
    #: the edge, and never joined or filtered on.
    public_addresses: list[Any] = Field(default_factory=list, sa_type=JSON)
    ssh_sources: list[Any] = Field(default_factory=list, sa_type=JSON)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class AnsibleSettingRow(Base, table=True):
    """Non-secret, fleet-wide Ansible policy.

    ``value`` is JSON because the whole point is that an operator sets an
    arbitrary Ansible variable — a bool, an int, a list of CIDRs. A column per
    setting would mean a migration per setting.
    """

    __tablename__ = "ansible_settings"

    name: str = Field(sa_column=Column(String, primary_key=True))
    value: dict[str, Any] = Field(sa_type=JSON)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class DeploymentRow(Base, table=True):
    """Convergence history.

    ``result`` is the JSON of one :class:`~blitzecdn.core.runs.AnsibleRun`:
    per-host counters, the tasks that changed, the tasks that failed. Raw
    stdout and stderr are deliberately not here — they are the largest thing a
    run produces and every reader would have to re-parse them to learn
    anything. They live in a log file the result names, outside the database.
    """

    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', "
            "'timed_out', 'abandoned')",
            name="deployments_status_check",
        ),
        CheckConstraint(
            "rollback_of IS NULL OR rollback_of != id",
            name="deployments_no_self_rollback_check",
        ),
    )

    id: str = Field(sa_column=Column(String, primary_key=True))
    status: str = Field(index=True)
    operator: str
    check_mode: bool
    rollback_of: str | None = Field(default=None, foreign_key="deployments.id")
    host_limit: str | None = None
    created_at: datetime = Field(
        default_factory=utcnow, sa_type=UtcDateTime, index=True
    )
    started_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    finished_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    result: dict[str, Any] | None = Field(default=None, sa_type=JSON)
    #: The desired state this deployment converged, and can roll back to.
    #: Opaque here on purpose — its shape is versioned by
    #: :mod:`blitzecdn.capabilities.deployments.snapshots`, not by this schema.
    snapshot: str
    #: For a rollback: a digest of the canonical desired state as it stood when
    #: this rollback was queued. Adoption compares it against canonical state
    #: again and refuses if it moved, because ``replace_all_records`` restores
    #: wholesale and would otherwise delete a record written while the rollback
    #: was converging — silently, with nothing left to say it existed.
    #:
    #: A digest rather than a second snapshot: these rows already carry a full
    #: copy of desired state, and all this needs to answer is "the same or not".
    #: ``NULL`` on an ordinary deployment, which adopts nothing.
    canonical_digest: str | None = None


class AuditEventRow(Base, table=True):
    """The append-only record of who changed what."""

    __tablename__ = "audit_events"

    id: int | None = Field(
        default=None,
        sa_column=Column(Integer, primary_key=True, autoincrement=True),
    )
    created_at: datetime = Field(
        default_factory=utcnow, sa_type=UtcDateTime, index=True
    )
    operator: str
    action: str
    resource_type: str
    resource_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)


class WorkflowRow(Base, table=True):
    """Crash-visible progress for work that crosses out of this transaction."""

    __tablename__ = "workflows"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('deployment', 'rollback', 'certificate')",
            name="workflows_kind_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'needs_review')",
            name="workflows_status_check",
        ),
        Index("workflows_status_idx", "status", "updated_at"),
    )

    id: str = Field(sa_column=Column(String, primary_key=True))
    kind: str
    resource_id: str | None = None
    status: str
    operator: str
    created_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
    #: An append-only list of checkpoints. Ordered, read whole, never queried
    #: into — a child table would buy nothing but joins.
    steps: list[Any] = Field(default_factory=list, sa_type=JSON)
    error: str | None = None


class ProjectionStateRow(Base, table=True):
    """What revision of the source a derived table was last built from."""

    __tablename__ = "projection_state"

    name: str = Field(sa_column=Column(String, primary_key=True))
    source_revision: str
    projected_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class DeploymentRequirementRow(Base, table=True):
    """Durable reasons the current desired state must reach the fleet."""

    __tablename__ = "deployment_requirements"

    kind: str = Field(sa_column=Column(String, primary_key=True))
    requested_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
