from __future__ import annotations

from pathlib import Path
from typing import Protocol

from blitzecdn.core.operation_ports import EventRecorder
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.features.certificates.domain import (
    CertificateInfo,
    CertificateSource,
    PreflightReport,
)
from blitzecdn.features.deployments.ports import (
    DeploymentGateway,
    DeploymentRequirements,
    DeploymentRunner,
)
from blitzecdn.features.dns.ports import SiteStore, ZoneEditor
from blitzecdn.features.dns.site_domain import CdnSite


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
