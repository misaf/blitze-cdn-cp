"""Invocation as data, shared by the CLI and the HTTP API.

Both entry layers do the same work in the same order: translate their
transport's arguments — a Typer flag, a JSON body — into a request, run it, and
translate the outcome back into an exit code or a status code. The commands
here are the middle of that loop: a parameter object and the facade call that
belongs to it. A CLI flag and an HTTP field that mean the same thing build the
same command, so a behaviour change lands once, in the command, instead of
twice, in the two entry layers.

Commands are deliberately thin. They hold parameters the entry layer has
already parsed and validated, call the facade, and return its models untouched.
Anything beyond the transport boundary — the policy, the locking, the stores —
belongs to the services and stays out of here. The facade surface they drive
is declared structurally (:class:`Facade`) so this layer never imports the
composition root.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from blitzecdn.domain.cache import CacheStatsReport, PurgeEntry, PurgeResult
from blitzecdn.domain.certificates import (
    CERTIFICATE_RENEWAL_DAYS,
    CertificateInfo,
    PreflightReport,
)
from blitzecdn.domain.deployments import Deployment, DriftReport
from blitzecdn.domain.dns import DnsRecord, Domain, RecordPatch, RecordType
from blitzecdn.domain.origins import OriginCheck
from blitzecdn.domain.runs import HostRun


class Facade(Protocol):
    """The slice of the control-plane facade the commands drive.

    Declared structurally, matching the methods each command calls, so
    ``application`` depends on an interface it names rather than on the
    composition root. The concrete object is always a
    :class:`~blitzecdn.control_plane.ControlPlane`.
    """

    def create_domain(self, domain: Domain, operator: str) -> Domain: ...
    def delete_domain(self, name: str, operator: str) -> None: ...
    def create_record(self, record: DnsRecord, operator: str) -> DnsRecord: ...
    def update_record(
        self,
        domain: str,
        name: str,
        type_: RecordType,
        patch: RecordPatch,
        operator: str,
    ) -> DnsRecord: ...
    def delete_record(
        self, domain: str, name: str, type_: RecordType, operator: str
    ) -> None: ...

    def upload_certificate(
        self, name: str, certificate_pem: bytes, private_key_pem: bytes, operator: str
    ) -> CertificateInfo: ...
    def request_certificate(
        self,
        name: str,
        operator: str,
        email: str | None = None,
        *,
        skip_preflight: bool = False,
    ) -> CertificateInfo: ...
    def certificate_preflight(self, name: str) -> PreflightReport: ...
    def renew_certificates(
        self,
        operator: str,
        *,
        within_days: int = CERTIFICATE_RENEWAL_DAYS,
        force: bool = False,
        sites: Sequence[str] | None = None,
        budget_seconds: float | None = None,
    ) -> dict[str, list[str]]: ...
    def reconcile_certificates(self, operator: str) -> dict[str, object]: ...

    def validate(self) -> list[str]: ...
    def deploy(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment: ...
    def submit_deployment(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment: ...
    def rollback(
        self, operator: str, deployment_id: str | None = None, *, check: bool = False
    ) -> Deployment: ...
    def submit_rollback(
        self, operator: str, deployment_id: str | None = None, *, check: bool = False
    ) -> Deployment: ...
    def check_drift(
        self, operator: str, *, host_limit: str | None = None
    ) -> DriftReport: ...

    def check_origins(self) -> list[OriginCheck]: ...
    def purge_cache(
        self,
        operator: str,
        *,
        entries: Sequence[PurgeEntry] = (),
        purge_all: bool = False,
        host_limit: str | None = None,
    ) -> PurgeResult: ...
    def cache_stats(
        self, operator: str, *, host_limit: str | None = None
    ) -> CacheStatsReport: ...
    def decommission_edge(
        self, name: str, operator: str, *, force: bool = False
    ) -> tuple[HostRun, ...]: ...


class Command(Protocol):
    """Something a transport can run. ``operator`` is who asked."""

    def execute(self, control_plane: Facade, operator: str) -> object: ...


# ----------------------------------------------------------------------
# Zones and records
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CreateDomainCommand:
    """Register a DNS zone delegated to BlitzeCDN."""

    name: str

    def execute(self, control_plane: Facade, operator: str) -> Domain:
        return control_plane.create_domain(Domain(name=self.name), operator)


@dataclass(frozen=True)
class DeleteDomainCommand:
    """Remove a zone and every record in it."""

    name: str

    def execute(self, control_plane: Facade, operator: str) -> None:
        control_plane.delete_domain(self.name, operator)


@dataclass(frozen=True)
class CreateRecordCommand:
    """Add a DNS record; a proxied one becomes an edge virtual host."""

    record: DnsRecord

    def execute(self, control_plane: Facade, operator: str) -> DnsRecord:
        return control_plane.create_record(self.record, operator)


@dataclass(frozen=True)
class UpdateRecordCommand:
    """Change one record's fields (origin, cache policy, firewall, proxy)."""

    domain: str
    name: str
    type_: RecordType
    patch: RecordPatch

    def execute(self, control_plane: Facade, operator: str) -> DnsRecord:
        return control_plane.update_record(
            self.domain, self.name, self.type_, self.patch, operator
        )


@dataclass(frozen=True)
class DeleteRecordCommand:
    """Delete one record, and the edge site derived from it."""

    domain: str
    name: str
    type_: RecordType

    def execute(self, control_plane: Facade, operator: str) -> None:
        control_plane.delete_record(self.domain, self.name, self.type_, operator)


# ----------------------------------------------------------------------
# Certificates
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class UploadCertificateCommand:
    """Install an operator-supplied certificate for a site."""

    name: str
    certificate_pem: bytes
    private_key_pem: bytes

    def execute(self, control_plane: Facade, operator: str) -> CertificateInfo:
        return control_plane.upload_certificate(
            self.name, self.certificate_pem, self.private_key_pem, operator
        )


@dataclass(frozen=True)
class RequestCertificateCommand:
    """Issue an ACME certificate for a site and activate it."""

    name: str
    email: str | None = None
    skip_preflight: bool = False

    def execute(self, control_plane: Facade, operator: str) -> CertificateInfo:
        return control_plane.request_certificate(
            self.name, operator, self.email, skip_preflight=self.skip_preflight
        )


@dataclass(frozen=True)
class CertificatePreflightCommand:
    """Report whether issuance could succeed right now, contacting no CA."""

    name: str

    def execute(self, control_plane: Facade, operator: str) -> PreflightReport:
        return control_plane.certificate_preflight(self.name)


@dataclass(frozen=True)
class RenewCertificatesCommand:
    """Reissue ACME certificates close to expiry, bounded by a budget."""

    within_days: int = CERTIFICATE_RENEWAL_DAYS
    force: bool = False
    sites: Sequence[str] | None = None
    budget_seconds: float | None = None

    def execute(self, control_plane: Facade, operator: str) -> dict[str, list[str]]:
        return control_plane.renew_certificates(
            operator,
            within_days=self.within_days,
            force=self.force,
            sites=self.sites,
            budget_seconds=self.budget_seconds,
        )


@dataclass(frozen=True)
class ReconcileCertificatesCommand:
    """Issue ready first certificates and install them with one deployment."""

    def execute(self, control_plane: Facade, operator: str) -> dict[str, object]:
        return control_plane.reconcile_certificates(operator)


# ----------------------------------------------------------------------
# Deployment
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ValidateCommand:
    """Answer whether desired state is coherent and the play parses."""

    def execute(self, control_plane: Facade, operator: str) -> list[str]:
        return control_plane.validate()


@dataclass(frozen=True)
class DeployCommand:
    """Converge the edges now, returning once the run has finished."""

    check: bool = False
    host_limit: str | None = None

    def execute(self, control_plane: Facade, operator: str) -> Deployment:
        return control_plane.deploy(
            operator, check=self.check, host_limit=self.host_limit
        )


@dataclass(frozen=True)
class SubmitDeploymentCommand:
    """Queue a convergence on a worker thread and return the queued record."""

    check: bool = False
    host_limit: str | None = None

    def execute(self, control_plane: Facade, operator: str) -> Deployment:
        return control_plane.submit_deployment(
            operator, check=self.check, host_limit=self.host_limit
        )


@dataclass(frozen=True)
class RollbackCommand:
    """Converge a prior snapshot and adopt it as canonical desired state."""

    deployment_id: str | None = None
    check: bool = False

    def execute(self, control_plane: Facade, operator: str) -> Deployment:
        return control_plane.rollback(operator, self.deployment_id, check=self.check)


@dataclass(frozen=True)
class SubmitRollbackCommand:
    """Queue a rollback on a worker thread and return the queued record."""

    deployment_id: str | None = None
    check: bool = False

    def execute(self, control_plane: Facade, operator: str) -> Deployment:
        return control_plane.submit_rollback(
            operator, self.deployment_id, check=self.check
        )


@dataclass(frozen=True)
class CheckDriftCommand:
    """Ask the fleet whether it still matches the declared desired state."""

    host_limit: str | None = None

    def execute(self, control_plane: Facade, operator: str) -> DriftReport:
        return control_plane.check_drift(operator, host_limit=self.host_limit)


# ----------------------------------------------------------------------
# Edge operations
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PurgeCacheCommand:
    """Remove cached responses from the edges."""

    entries: Sequence[PurgeEntry] = field(default_factory=tuple)
    purge_all: bool = False
    host_limit: str | None = None

    def execute(self, control_plane: Facade, operator: str) -> PurgeResult:
        return control_plane.purge_cache(
            operator,
            entries=self.entries,
            purge_all=self.purge_all,
            host_limit=self.host_limit,
        )


@dataclass(frozen=True)
class CheckOriginsCommand:
    """Connect to every enabled site's origin the way the edge will."""

    def execute(self, control_plane: Facade, operator: str) -> list[OriginCheck]:
        return control_plane.check_origins()


@dataclass(frozen=True)
class CacheStatsCommand:
    """Collect cache effectiveness from the edges."""

    host_limit: str | None = None

    def execute(self, control_plane: Facade, operator: str) -> CacheStatsReport:
        return control_plane.cache_stats(operator, host_limit=self.host_limit)


@dataclass(frozen=True)
class DecommissionEdgeCommand:
    """Strip an edge of BlitzeCDN state, then take it out of inventory."""

    name: str
    force: bool = False

    def execute(self, control_plane: Facade, operator: str) -> tuple[HostRun, ...]:
        return control_plane.decommission_edge(self.name, operator, force=self.force)


__all__ = [
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
    "Facade",
    "PurgeCacheCommand",
    "ReconcileCertificatesCommand",
    "RenewCertificatesCommand",
    "RequestCertificateCommand",
    "RollbackCommand",
    "SubmitDeploymentCommand",
    "SubmitRollbackCommand",
    "UpdateRecordCommand",
    "UploadCertificateCommand",
    "ValidateCommand",
]
