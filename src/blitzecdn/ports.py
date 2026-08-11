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

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from blitzecdn.domain.audit import AuditEvent
from blitzecdn.domain.certificates import (
    CertificateInfo,
    CertificateSource,
    PreflightReport,
)
from blitzecdn.domain.deployments import Deployment, DeploymentStatus
from blitzecdn.domain.dns import DnsRecord, Domain, RecordType
from blitzecdn.domain.origins import OriginCheck
from blitzecdn.domain.runs import AnsibleRun
from blitzecdn.domain.sites import CdnSite

# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------


class SiteStore(Protocol):
    """The derived virtual hosts. Written only by re-derivation from records."""

    def list_sites(self) -> list[CdnSite]: ...

    def get_site(self, name: str) -> CdnSite: ...

    def replace_all_sites(self, sites: list[CdnSite]) -> None: ...


class ZoneStore(Protocol):
    """Zones and records — the source of truth sites are derived from."""

    def list_domains(self) -> list[Domain]: ...

    def get_domain(self, name: str) -> Domain: ...

    def create_domain(self, domain: Domain) -> Domain: ...

    def delete_domain(self, name: str) -> None: ...

    def list_records(self, domain: str | None = None) -> list[DnsRecord]: ...

    def get_record(self, domain: str, name: str, type_: RecordType) -> DnsRecord: ...

    def create_record(self, record: DnsRecord) -> DnsRecord: ...

    def replace_record(self, record: DnsRecord) -> DnsRecord: ...

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

    def abandon_running(self) -> int: ...

    def successful_rollback_target(self, current_snapshot: str) -> Deployment: ...


class AuditLog(Protocol):
    """Append-only record of who did what."""

    def audit(
        self,
        operator: str,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent: ...


# ----------------------------------------------------------------------
# Process execution
# ----------------------------------------------------------------------


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

    def run_decommission(self, *, host_limit: str) -> AnsibleRun: ...


class LogReader(Protocol):
    """Reads back a run log, for showing an operator what Ansible said.

    Narrow on purpose. Application code may quote a log into a message; it may
    not branch on one, and a port with a single tail-reading method is what
    keeps that distinction enforceable rather than merely intended.
    """

    def __call__(self, path: Path | str | None, *, limit: int) -> str: ...


class EdgeInventory(Protocol):
    """The inventory file, as the application needs to read and edit it."""

    def list_edges(self) -> list[dict[str, str]]: ...

    def remove_edge(self, name: str) -> None: ...


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
    def check(self, site: CdnSite) -> OriginCheck: ...

    def check_all(self, sites: list[CdnSite]) -> list[OriginCheck]: ...


__all__ = [
    "AuditLog",
    "CertificateStore",
    "DeploymentRunner",
    "DeploymentStore",
    "EdgeInventory",
    "Issuer",
    "LogReader",
    "OriginProbe",
    "Preflight",
    "SiteStore",
    "ZoneStore",
]
