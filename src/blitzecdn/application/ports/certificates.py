from __future__ import annotations

from pathlib import Path
from typing import Protocol

from blitzecdn.application.ports.common import UnitOfWork
from blitzecdn.application.ports.deployments import (
    DeploymentGateway,
    DeploymentRequirements,
    DeploymentRunner,
)
from blitzecdn.application.ports.dns import SiteStore, ZoneEditor
from blitzecdn.application.ports.operations import EventRecorder
from blitzecdn.domain.certificates import (
    CertificateInfo,
    CertificateSource,
    PreflightReport,
)
from blitzecdn.domain.sites import CdnSite


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


__all__ = [
    "CertificateStore",
    "DeploymentGateway",
    "DeploymentRequirements",
    "DeploymentRunner",
    "EventRecorder",
    "Issuer",
    "Preflight",
    "SiteStore",
    "UnitOfWork",
    "ZoneEditor",
]
