from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from blitzecdn.core.operation_ports import EventRecorder
from blitzecdn.core.operations import WorkflowKind
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.features.deployments.ports import (
    DeploymentGateway,
    DeploymentLocker,
    DeploymentRequirements,
)
from blitzecdn.features.dns.ports import ZoneEditor
from blitzecdn.features.sites.domain import CdnSite
from blitzecdn.features.sites.ports import SiteReader
from blitzecdn_certificates.certificates.domain import (
    CertificateInfo,
    CertificateSource,
    PreflightReport,
)


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
    "SiteReader",
    "UnitOfWork",
    "WorkflowCoordinator",
    "WorkflowProgress",
    "ZoneEditor",
]
