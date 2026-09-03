from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from blitzecdn.core.operation_ports import EventRecorder
from blitzecdn.core.operations import WorkflowKind
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.capabilities.deployments.ports import (
    DeploymentGateway,
    DeploymentLocker,
    DeploymentRequirements,
)
from blitzecdn.capabilities.dns.domain import DnsRecord
from blitzecdn.capabilities.sites.domain import CdnSite
from blitzecdn.capabilities.sites.ports import SiteReader
from blitzecdn.capabilities.tls.policy import CertificateMode, SslMode
from blitzecdn_certificates.certificates.domain import (
    CertificateInfo,
    CertificateSource,
    PreflightReport,
)


class SiteEditor(Protocol):
    """The one site write this capability performs.

    Certificate state used to be written onto a DNS *record*, because the site
    it derived was a projection that any record change would overwrite. Sites
    are canonical now, so this is an ordinary site update and the port says so.

    Two methods out of ``SiteService``'s eight, declared here because this
    package is the consumer: the control plane publishes the service and this
    is the slice of it certificates may reach.
    """

    def activate_managed_certificate(
        self, site: CdnSite, mode: CertificateMode
    ) -> CdnSite: ...

    def apply_automatic_ssl_upgrade(
        self, site_name: str, target: SslMode, operator: str
    ) -> CdnSite | None: ...


class RecordReader(Protocol):
    """Finding a record routed to a site, for its TTL.

    Preflight compares the record's TTL against how long a validation may take,
    so this stays pointed at `dns` — it is genuinely a question about DNS and
    not about the site.
    """

    def record_for_site(self, site_name: str) -> DnsRecord: ...


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


class WorkflowProgress(Protocol):
    def checkpoint(
        self, name: str, details: dict[str, Any] | None = None
    ) -> object: ...


class WorkflowRun(Protocol):
    def __enter__(self) -> WorkflowProgress: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class WorkflowCoordinator(Protocol):
    def run(
        self, kind: WorkflowKind, operator: str, resource_id: str | None = None
    ) -> WorkflowRun: ...


__all__ = [
    "CertificateStore",
    "DeploymentGateway",
    "DeploymentLocker",
    "DeploymentRequirements",
    "EventRecorder",
    "Issuer",
    "Preflight",
    "RecordReader",
    "SiteEditor",
    "SiteReader",
    "UnitOfWork",
    "WorkflowCoordinator",
    "WorkflowProgress",
]
