from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from blitzecdn.capabilities.deployments.ports import (
    DeploymentGateway,
    DeploymentLocker,
    DeploymentRequirements,
)
from blitzecdn.capabilities.dns.domain import CdnSite, DnsRecord
from blitzecdn.capabilities.dns.ports import SiteReader
from blitzecdn.capabilities.tls.policy import CertificateMode, SslMode
from blitzecdn.capabilities.workflows.domain import WorkflowKind
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder
from blitzecdn_certificates.certificates.domain import (
    CertificateInfo,
    CertificateSource,
    PreflightReport,
)


class SiteEditor(Protocol):
    """The one write this capability performs, and it is not to a site.

    A virtual host is derived from a zone, its rules and its records, so there
    is nothing to update on the host itself. These two record their result
    against whatever *authored* the host — the zone, or the rule that bent it
    — and the zone editor is what knows which. That the two methods still take
    a host is right: the host is what a certificate was issued for.

    Two methods out of the zone editor's many, declared here because this
    package is the consumer.
    """

    def activate_managed_certificate(
        self, site: CdnSite, mode: CertificateMode
    ) -> CdnSite: ...

    def apply_automatic_ssl_upgrade(
        self, site_name: str, target: SslMode, operator: str
    ) -> CdnSite | None: ...


class RecordReader(Protocol):
    """Finding a record for a hostname, for its TTL.

    Preflight compares the record's TTL against how long a validation may take,
    so this stays pointed at `dns` — it is genuinely a question about DNS and
    not about the host.

    It asks by hostname rather than by host name now. A host is a group of
    hostnames that resolve alike, and "a record routed to this site" stopped
    being a stored relationship anything could look up.
    """

    def record_for_hostname(self, fqdn: str) -> DnsRecord: ...


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
