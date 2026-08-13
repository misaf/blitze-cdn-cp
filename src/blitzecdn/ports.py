"""The interfaces the application layer depends on.

Every collaborator the application needs is declared here as a ``Protocol``, so
``application/`` imports this module and the domain and nothing else. The
concrete adapters in ``infrastructure/`` are matched structurally — they never
import or subclass these — which keeps the dependency arrow pointing inward and
lets a test double satisfy a port by shape alone.

The ports are narrow on purpose. A service asks for the handful of methods it
actually calls rather than for a whole repository, so the constructor of each
service documents its real reach into persistence.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from blitzecdn.domain.audit import AuditEvent
from blitzecdn.domain.certificates import (
    CertificateInfo,
    CertificateSource,
    PreflightReport,
)
from blitzecdn.domain.deployments import Deployment, DeploymentStatus
from blitzecdn.domain.dns import DnsRecord, Domain, RecordType
from blitzecdn.domain.edges import Edge
from blitzecdn.domain.events import DomainEvent
from blitzecdn.domain.operations import (
    Workflow,
    WorkflowKind,
    WorkflowStatus,
    WorkflowStep,
)
from blitzecdn.domain.origins import OriginCheck
from blitzecdn.domain.runs import AnsibleRun
from blitzecdn.domain.sites import CdnSite, CertificateMode

# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------


class UnitOfWork(Protocol):
    """One atomic boundary shared by every store used by a use case."""

    def transaction(self) -> AbstractContextManager[Any]: ...


class DeploymentRequirements(Protocol):
    def require(self, kind: str) -> None: ...

    def clear(self, kind: str) -> None: ...

    def pending(self, kind: str) -> bool: ...


class SiteStore(Protocol):
    """The derived virtual hosts. Written only by re-derivation from records."""

    def list_sites(self) -> list[CdnSite]: ...

    def get_site(self, name: str) -> CdnSite: ...

    def replace_all_sites(self, sites: list[CdnSite]) -> None: ...

    def projection_revision(self) -> str | None: ...

    def set_projection_revision(self, revision: str) -> None: ...


class ZoneStore(Protocol):
    """Zones and records — the source of truth sites are derived from."""

    def list_domains(self) -> list[Domain]: ...

    def get_domain(self, name: str) -> Domain: ...

    def create_domain(self, domain: Domain) -> Domain: ...

    def delete_domain(self, name: str) -> None: ...

    def list_records(self, domain: str | None = None) -> list[DnsRecord]: ...

    def get_record(self, domain: str, name: str, type_: RecordType) -> DnsRecord: ...

    def create_record(self, record: DnsRecord) -> DnsRecord: ...

    def replace_record(
        self, record: DnsRecord, *, expected: DnsRecord | None = None
    ) -> DnsRecord: ...

    def delete_record(self, domain: str, name: str, type_: RecordType) -> None: ...

    def replace_all_records(
        self, domains: list[Domain], records: list[DnsRecord]
    ) -> None: ...


class DeploymentStore(Protocol):
    """Deployment history and the snapshots it converges."""

    def snapshot(self) -> str: ...

    def create_deployment(
        self,
        operator: str,
        *,
        check_mode: bool,
        rollback_of: str | None = None,
        snapshot: str | None = None,
        host_limit: str | None = None,
        canonical_digest: str | None = None,
    ) -> Deployment: ...

    def transition(
        self,
        deployment_id: str,
        expected: DeploymentStatus,
        target: DeploymentStatus,
        **values: Any,
    ) -> Deployment: ...

    def get_deployment(self, deployment_id: str) -> Deployment: ...

    def deployment_snapshot(self, deployment_id: str) -> str: ...

    def list_deployments(self, limit: int = 20) -> list[Deployment]: ...

    def queued_deployments(self) -> list[Deployment]: ...

    def abandon_running(self, *, include_queued: bool = True) -> int: ...

    def prune_history(self, keep: int) -> int: ...

    def successful_rollback_target(self, current_snapshot: str) -> Deployment: ...


class AuditTrail(Protocol):
    """The append-only operator log, as the entry layers need to read it.

    Read-only on purpose. The trail is *written* by the subscriber the
    composition root registers on the bus (see :class:`EventBus`), never by a
    caller — so a port that offered an ``audit`` method would be an invitation
    to record something no domain event ever announced.
    """

    def get_audit_event(self, event_id: int) -> AuditEvent: ...

    def list_audit_events(self, limit: int = 100) -> list[AuditEvent]: ...


class EventBus(Protocol):
    """Where the application publishes that something happened.

    A service that changes state publishes a :class:`DomainEvent` here and
    moves on; it never learns who is listening. Subscribers are wired by the
    composition root, so adding one never touches the service that announced
    the change.
    """

    def publish(self, event: DomainEvent) -> None: ...


class WorkflowJournal(Protocol):
    def create(
        self,
        workflow_id: str,
        kind: WorkflowKind,
        operator: str,
        resource_id: str | None = None,
    ) -> Workflow: ...

    def advance(
        self,
        workflow_id: str,
        status: WorkflowStatus,
        *,
        step: WorkflowStep | None = None,
        error: str | None = None,
    ) -> Workflow: ...

    def unfinished(self) -> list[Workflow]: ...

    def prune_finished(self, keep: int) -> int: ...

    def get(self, workflow_id: str) -> Workflow: ...

    def list_workflows(self, limit: int = 100) -> list[Workflow]: ...


# ----------------------------------------------------------------------
# Services, as their siblings see them
# ----------------------------------------------------------------------
#
# The application services form a DAG (see ``application/__init__.py``), so a
# few of them legitimately depend on one another. They are declared here for
# the same reason everything else is: a service should name the handful of
# methods it calls on a collaborator rather than take the collaborator whole.
# Narrow enough that a test can satisfy one without building the real service
# behind it, which taking the collaborator whole would make impossible.


class ZoneEditor(Protocol):
    """What the other services need from the zone editor.

    Four methods out of ``DnsService``'s twenty. Certificate state has to land
    on the *record* rather than on the derived site, so issuing a certificate
    unavoidably reaches back into the zone editor — but only this far.
    """

    def sync_sites(self) -> None: ...

    #: Ways the stored zones and their derived sites contradict each other.
    #: A deploy asks before it converges anything, because the contradictions
    #: are the kind that would otherwise reach an edge as a valid-looking
    #: config serving the wrong site.
    def validation_errors(self) -> list[str]: ...

    def record_for_site(self, site_name: str) -> DnsRecord: ...

    def activate_managed_certificate(
        self, site: CdnSite, mode: CertificateMode
    ) -> CdnSite: ...


class DeploymentGateway(Protocol):
    """What the certificate service needs from the deployment service.

    Issuance asks whether a site is actually on the edges (HTTP-01 cannot
    validate a vhost nothing serves), and reconciliation installs what it
    issued. Nothing else.
    """

    def site_is_deployed(self, site_name: str) -> bool: ...

    def deploy(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment: ...


# ----------------------------------------------------------------------
# Process execution
# ----------------------------------------------------------------------


class BackgroundRunner(Protocol):
    """Starts work that outlives the call that asked for it.

    The queued deploy and rollback endpoints answer before the run finishes, so
    something has to carry the run onwards. That something is a concrete
    concurrency mechanism, which makes it an adapter — the application layer
    injects it like every other, and a test can supply a runner that executes
    inline and observe a whole queued deployment synchronously.

    ``start`` must raise if the work will not be run. The caller owns the
    deployment lock at that moment and releases it on failure; a runner that
    swallowed the error would leave the lock held by a worker that does not
    exist, and every later deploy would fail until the process restarted.
    """

    def start(self, work: Callable[[], None], *, name: str) -> None: ...


@runtime_checkable
class QueueBackgroundRunner(Protocol):
    """Enqueues a durable identifier for an out-of-process worker."""

    def enqueue(self, deployment_id: str) -> None: ...


class DeploymentRunner(Protocol):
    """Runs Ansible and owns the cross-process deployment lock.

    Every method answers with an :class:`~blitzecdn.domain.runs.AnsibleRun`,
    which is the whole of what the application layer learns about a run. There
    is deliberately no way through this port to reach the raw output.
    """

    def lock(self) -> AbstractContextManager[Any]: ...

    #: ``variables`` is supplied rather than assumed so validation never writes
    #: over the desired-state file a concurrent deploy is converging.
    def validate(self, variables: Path) -> AnsibleRun: ...

    def run(self, *, check: bool, host_limit: str | None = None) -> AnsibleRun: ...

    def run_cache_purge(
        self,
        *,
        entries: list[dict[str, str]],
        purge_all: bool,
        host_limit: str | None = None,
    ) -> AnsibleRun: ...

    def run_stats(self, *, host_limit: str | None = None) -> AnsibleRun: ...

    #: ``sites`` is passed rather than read from the desired-state file: this
    #: takes no deployment lock, so that file may belong to a deploy in flight.
    def run_origin_check(
        self, *, sites: list[dict[str, object]], host_limit: str | None = None
    ) -> AnsibleRun: ...

    def run_decommission(self, *, host_limit: str) -> AnsibleRun: ...


class LogReader(Protocol):
    """Reads back a run log, for showing an operator what Ansible said.

    Narrow on purpose. Application code may quote a log into a message; it may
    not branch on one, and a port with a single tail-reading method is what
    keeps that distinction enforceable rather than merely intended.
    """

    def __call__(self, path: Path | str | None, *, limit: int) -> str: ...


class YamlWriter(Protocol):
    """Publishes the desired-state document Ansible reads.

    A port rather than a bare ``Callable`` so it reads like its neighbours and
    can carry the one thing about it that matters: the write has to be atomic.
    A deploy renders this file while holding the deployment lock and Ansible
    reads it moments later, so a reader must never observe a partial document —
    an edge converged from half a desired state is worse than one that did not
    converge at all.

    Positional-only, because the parameters of a function-shaped port are an
    implementation's business: the adapter may name its second argument
    ``payload`` and take extra keyword arguments with defaults, and none of
    that is something a caller here should have to match.
    """

    def __call__(self, path: Path, document: dict[str, object], /) -> None: ...


class EdgeStore(Protocol):
    """The fleet. The Ansible inventory is derived from this, not the reverse.

    The ``blitzecdn`` inventory plugin reads these same rows at the start of
    every run, so writing an edge here *is* publishing it to Ansible. There is
    no second artefact that could disagree with the database about which hosts
    exist.
    """

    def list_edges(self) -> list[Edge]: ...

    def get_edge(self, name: str) -> Edge: ...

    def create_edge(self, edge: Edge) -> Edge: ...

    def replace_edge(self, edge: Edge, *, expected: Edge | None = None) -> Edge: ...

    def delete_edge(self, name: str) -> None: ...


# ----------------------------------------------------------------------
# Certificates
# ----------------------------------------------------------------------


class Issuer(Protocol):
    def issue(self, site: CdnSite, email: str) -> tuple[bytes, bytes]: ...


class CertificateStore(Protocol):
    def install(
        self,
        site: CdnSite,
        certificate_pem: bytes,
        private_key_pem: bytes,
        *,
        source: CertificateSource,
        email: str | None = None,
    ) -> CertificateInfo: ...

    def get(self, site_name: str) -> CertificateInfo: ...

    def list_all(self) -> list[CertificateInfo]: ...

    def sources(self, site_name: str) -> tuple[Path, Path]: ...


class Preflight(Protocol):
    def check(
        self, site: CdnSite, *, deployed: bool, record_ttl: int | None = None
    ) -> PreflightReport: ...


class OriginProbe(Protocol):
    """A site's origin: how to reach it, and what the controller sees of it.

    ``to_probe`` renders an origin for whoever is going to connect to it. The
    operator-facing check is answered by the *edges* — ``check_origins`` runs a
    playbook, because the controller's routes, resolver and egress rules are not
    the ones that carry traffic, and an origin allow-listing the edges refuses
    the controller while working perfectly.

    ``check`` is the controller connecting for itself, and survives for exactly
    one caller: the advisory origin check inside certificate preflight, which
    has to answer in milliseconds during issuance and cannot run a playbook. It
    is advisory there precisely because of the vantage point.
    """

    def to_probe(self, site: CdnSite) -> dict[str, object]: ...

    def check(self, site: CdnSite) -> OriginCheck: ...


__all__ = [
    "AuditTrail",
    "BackgroundRunner",
    "CertificateStore",
    "DeploymentGateway",
    "DeploymentRunner",
    "DeploymentStore",
    "EdgeStore",
    "EventBus",
    "Issuer",
    "LogReader",
    "OriginProbe",
    "Preflight",
    "QueueBackgroundRunner",
    "SiteStore",
    "WorkflowJournal",
    "YamlWriter",
    "ZoneEditor",
    "ZoneStore",
]
