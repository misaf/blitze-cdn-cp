from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from blitzecdn.capabilities.deployments.ports import (
    DeploymentGateway,
    DeploymentLocker,
    DeploymentRequirements,
)
from blitzecdn.capabilities.dns.domain import DnsRecord, SiteChange
from blitzecdn.capabilities.dns.ports import SiteReader
from blitzecdn.capabilities.releases.domain import Release, ReleaseInputs
from blitzecdn.capabilities.tls.certificates.domain import (
    CertificateInfo,
    CertificateSource,
    PreflightReport,
)
from blitzecdn.capabilities.tls.contract import (
    CertificateMode,
    CertificateVersion,
    SslMode,
)
from blitzecdn.capabilities.workflows.domain import WorkflowKind
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder


class SiteWriter(Protocol):
    """DNS's one write to the zone or rule behind a host; see `HostService`."""

    def change_site(
        self, site_name: str, decide: Callable[[CdnSite], SiteChange | None]
    ) -> CdnSite | None: ...


class SiteEditor(Protocol):
    """Record issuance results against the zone or rule that authored a host."""

    def activate_managed_certificate(
        self, site: CdnSite, mode: CertificateMode, version: CertificateVersion
    ) -> CdnSite: ...

    def apply_automatic_ssl_upgrade(
        self, site_name: str, target: SslMode, operator: str
    ) -> CdnSite | None: ...


class RecordReader(Protocol):
    """Finding a record for a hostname, for preflight's TTL check."""

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

    def collect(self, referenced_paths: Sequence[str]) -> tuple[str, ...]: ...

    def sources(
        self, site_name: str, *, version: CertificateVersion | None = None
    ) -> tuple[Path, Path]: ...


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
    "MaterialReferences",
    "Preflight",
    "RecordReader",
    "ReleaseHistory",
    "SiteEditor",
    "SiteReader",
    "SiteWriter",
    "UnitOfWork",
    "WorkflowCoordinator",
    "WorkflowProgress",
]


class MaterialReferences(Protocol):
    """Canonical and retained release roots, read under the unit of work."""

    def __call__(self) -> Sequence[str]: ...


class ReleaseHistory(Protocol):
    """Current canonical state, and the state each retained release was built from.

    Collection keeps every certificate version either still names.
    """

    def inputs(self) -> ReleaseInputs: ...

    def inputs_for(self, release_id: str) -> ReleaseInputs: ...

    def list_releases(self, limit: int | None = 20) -> Sequence[Release]: ...
