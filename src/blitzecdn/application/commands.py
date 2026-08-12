"""Invocation as data, shared by the CLI and the HTTP API.

Both entry layers do the same work in the same order: translate their
transport's arguments — a Typer flag, a JSON body — into a request, run it, and
translate the outcome back into an exit code or a status code. The commands
here are the middle of that loop: a parameter object and the service call
that belongs to it. A CLI flag and an HTTP field that mean the same thing build the
same command, so a behaviour change lands once, in the command, instead of
twice, in the two entry layers.

Commands are deliberately thin. They hold parameters the entry layer has
already parsed and validated, call the service that owns the work, and return
its models untouched. Anything beyond the transport boundary — the policy, the
locking, the stores — belongs to the services and stays out of here.

Each command reaches for one service off :class:`Services`, which declares
nothing but the four attributes and so restates no signature. That matters more
than it looks: a parameter this layer accepts is already written down twice, on
the command and on the service method it forwards to, and a third copy in an
interface here would be a copy nothing checks against the other two until a
caller passes the argument the mismatched one does not take.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from blitzecdn.application.certificates import CertificateService
from blitzecdn.application.deployments import DeploymentService
from blitzecdn.application.dns import DnsService
from blitzecdn.application.edges import EdgeOperationsService
from blitzecdn.domain.cache import CacheStatsReport, PurgeEntry, PurgeResult
from blitzecdn.domain.certificates import (
    CERTIFICATE_RENEWAL_DAYS,
    CertificateInfo,
    PreflightReport,
)
from blitzecdn.domain.deployments import Deployment, DriftReport
from blitzecdn.domain.dns import DnsRecord, Domain, RecordPatch, RecordType
from blitzecdn.domain.edges import Edge, EdgePatch
from blitzecdn.domain.origins import OriginCheck
from blitzecdn.domain.runs import HostRun


class Services(Protocol):
    """The application services, as a transport reaches them.

    Structural, so this layer names what it needs without importing the
    composition root — but the attributes are the concrete service classes,
    which are siblings in this same layer and therefore free to name. The
    object supplied is always a
    :class:`~blitzecdn.control_plane.ControlPlane`.
    """

    dns: DnsService
    certificates: CertificateService
    deployments: DeploymentService
    edges: EdgeOperationsService


class Command(Protocol):
    """Something a transport can run. ``operator`` is who asked."""

    def execute(self, services: Services, operator: str) -> object: ...


# ----------------------------------------------------------------------
# Zones and records
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CreateDomainCommand:
    """Register a DNS zone delegated to BlitzeCDN."""

    name: str

    def execute(self, services: Services, operator: str) -> Domain:
        return services.dns.create_domain(Domain(name=self.name), operator)


@dataclass(frozen=True)
class DeleteDomainCommand:
    """Remove a zone and every record in it."""

    name: str

    def execute(self, services: Services, operator: str) -> None:
        services.dns.delete_domain(self.name, operator)


@dataclass(frozen=True)
class CreateRecordCommand:
    """Add a DNS record; a proxied one becomes an edge virtual host."""

    record: DnsRecord

    def execute(self, services: Services, operator: str) -> DnsRecord:
        return services.dns.create_record(self.record, operator)


@dataclass(frozen=True)
class UpdateRecordCommand:
    """Change one record's fields (origin, cache policy, firewall, proxy)."""

    domain: str
    name: str
    type_: RecordType
    patch: RecordPatch

    def execute(self, services: Services, operator: str) -> DnsRecord:
        return services.dns.update_record(
            self.domain, self.name, self.type_, self.patch, operator
        )


@dataclass(frozen=True)
class DeleteRecordCommand:
    """Delete one record, and the edge site derived from it."""

    domain: str
    name: str
    type_: RecordType

    def execute(self, services: Services, operator: str) -> None:
        services.dns.delete_record(self.domain, self.name, self.type_, operator)


# ----------------------------------------------------------------------
# Certificates
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class UploadCertificateCommand:
    """Install an operator-supplied certificate for a site."""

    name: str
    certificate_pem: bytes
    private_key_pem: bytes

    def execute(self, services: Services, operator: str) -> CertificateInfo:
        return services.certificates.upload_certificate(
            self.name, self.certificate_pem, self.private_key_pem, operator
        )


@dataclass(frozen=True)
class RequestCertificateCommand:
    """Issue an ACME certificate for a site and activate it."""

    name: str
    email: str | None = None
    skip_preflight: bool = False

    def execute(self, services: Services, operator: str) -> CertificateInfo:
        return services.certificates.request_certificate(
            self.name, operator, self.email, skip_preflight=self.skip_preflight
        )


@dataclass(frozen=True)
class CertificatePreflightCommand:
    """Report whether issuance could succeed right now, contacting no CA."""

    name: str

    def execute(self, services: Services, operator: str) -> PreflightReport:
        return services.certificates.certificate_preflight(self.name)


@dataclass(frozen=True)
class RenewCertificatesCommand:
    """Reissue ACME certificates close to expiry, bounded by a budget."""

    within_days: int = CERTIFICATE_RENEWAL_DAYS
    force: bool = False
    sites: Sequence[str] | None = None
    budget_seconds: float | None = None

    def execute(self, services: Services, operator: str) -> dict[str, list[str]]:
        return services.certificates.renew_certificates(
            operator,
            within_days=self.within_days,
            force=self.force,
            sites=self.sites,
            budget_seconds=self.budget_seconds,
        )


@dataclass(frozen=True)
class ReconcileCertificatesCommand:
    """Issue ready first certificates and install them with one deployment."""

    def execute(self, services: Services, operator: str) -> dict[str, object]:
        return services.certificates.reconcile_certificates(operator)


# ----------------------------------------------------------------------
# Deployment
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ValidateCommand:
    """Answer whether desired state is coherent and the play parses."""

    def execute(self, services: Services, operator: str) -> list[str]:
        return services.deployments.validate()


@dataclass(frozen=True)
class DeployCommand:
    """Converge the edges now, returning once the run has finished."""

    check: bool = False
    host_limit: str | None = None

    def execute(self, services: Services, operator: str) -> Deployment:
        return services.deployments.deploy(
            operator, check=self.check, host_limit=self.host_limit
        )


@dataclass(frozen=True)
class SubmitDeploymentCommand:
    """Queue a convergence on a worker thread and return the queued record."""

    check: bool = False
    host_limit: str | None = None

    def execute(self, services: Services, operator: str) -> Deployment:
        return services.deployments.submit_deployment(
            operator, check=self.check, host_limit=self.host_limit
        )


@dataclass(frozen=True)
class RollbackCommand:
    """Converge a prior snapshot and adopt it as canonical desired state."""

    deployment_id: str | None = None
    check: bool = False

    def execute(self, services: Services, operator: str) -> Deployment:
        return services.deployments.rollback(
            operator, self.deployment_id, check=self.check
        )


@dataclass(frozen=True)
class SubmitRollbackCommand:
    """Queue a rollback on a worker thread and return the queued record."""

    deployment_id: str | None = None
    check: bool = False

    def execute(self, services: Services, operator: str) -> Deployment:
        return services.deployments.submit_rollback(
            operator, self.deployment_id, check=self.check
        )


@dataclass(frozen=True)
class CheckDriftCommand:
    """Ask the fleet whether it still matches the declared desired state."""

    host_limit: str | None = None

    def execute(self, services: Services, operator: str) -> DriftReport:
        return services.deployments.check_drift(operator, host_limit=self.host_limit)


# ----------------------------------------------------------------------
# Edge operations
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PurgeCacheCommand:
    """Remove cached responses from the edges."""

    entries: Sequence[PurgeEntry] = field(default_factory=tuple)
    purge_all: bool = False
    host_limit: str | None = None

    def execute(self, services: Services, operator: str) -> PurgeResult:
        return services.edges.purge_cache(
            operator,
            entries=self.entries,
            purge_all=self.purge_all,
            host_limit=self.host_limit,
        )


@dataclass(frozen=True)
class CheckOriginsCommand:
    """Connect to every enabled site's origin the way the edge will."""

    def execute(self, services: Services, operator: str) -> list[OriginCheck]:
        return services.edges.check_origins()


@dataclass(frozen=True)
class CacheStatsCommand:
    """Collect cache effectiveness from the edges."""

    host_limit: str | None = None

    def execute(self, services: Services, operator: str) -> CacheStatsReport:
        return services.edges.cache_stats(operator, host_limit=self.host_limit)


@dataclass(frozen=True)
class AddEdgeCommand:
    """Register an edge. Ansible sees it on the next run; nothing converges now."""

    edge: Edge

    def execute(self, services: Services, operator: str) -> Edge:
        return services.edges.add_edge(self.edge, operator)


@dataclass(frozen=True)
class UpdateEdgeCommand:
    """Change an edge's connection details or public addresses."""

    name: str
    patch: EdgePatch

    def execute(self, services: Services, operator: str) -> Edge:
        return services.edges.update_edge(self.name, self.patch, operator)


@dataclass(frozen=True)
class RemoveEdgeCommand:
    """Stop managing an edge without touching the host it names."""

    name: str

    def execute(self, services: Services, operator: str) -> None:
        services.edges.remove_edge(self.name, operator)


@dataclass(frozen=True)
class DecommissionEdgeCommand:
    """Strip an edge of BlitzeCDN state, then take it out of inventory."""

    name: str
    force: bool = False

    def execute(self, services: Services, operator: str) -> tuple[HostRun, ...]:
        return services.edges.decommission_edge(self.name, operator, force=self.force)


__all__ = [
    "AddEdgeCommand",
    "CacheStatsCommand",
    "CertificatePreflightCommand",
    "CheckDriftCommand",
    "CheckOriginsCommand",
    "Command",
    "CreateDomainCommand",
    "CreateRecordCommand",
    "DecommissionEdgeCommand",
    "DeleteDomainCommand",
    "DeleteRecordCommand",
    "DeployCommand",
    "PurgeCacheCommand",
    "ReconcileCertificatesCommand",
    "RenewCertificatesCommand",
    "RequestCertificateCommand",
    "RollbackCommand",
    "Services",
    "SubmitDeploymentCommand",
    "SubmitRollbackCommand",
    "UpdateRecordCommand",
    "UploadCertificateCommand",
    "ValidateCommand",
]
