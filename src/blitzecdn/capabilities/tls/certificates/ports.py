from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, NewType, Protocol

from blitzecdn.capabilities.deployments import (
    DeploymentGateway,
    DeploymentLocker,
    DeploymentRequirements,
)
from blitzecdn.capabilities.releases import Release, ReleaseInputs
from blitzecdn.capabilities.sites import DnsRecord, SiteChange, SiteReader
from blitzecdn.capabilities.tls.certificates.domain import (
    CertificateInfo,
    CertificateSource,
    ChallengeEdges,
    PreflightAttempt,
    PreflightReport,
)
from blitzecdn.capabilities.tls.contract import (
    CertificateMode,
    CertificateVersion,
    SslMode,
)
from blitzecdn.capabilities.workflows import WorkflowKind
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder

#: How long one ACME issuance may take. It runs under the deployment lock, so
#: the composition root hands it the deployment budget.
IssuanceTimeout = NewType("IssuanceTimeout", int)


class SiteWriter(Protocol):
    """DNS's one write to the zone or rule behind a host; see `HostService`."""

    def change_site(
        self, site_name: str, decide: Callable[[CdnSite], SiteChange | None]
    ) -> CdnSite | None:
        """Write what ``decide`` asks of a host, judged on the host as stored now.

        ``decide`` runs inside the transaction that writes, so a guard it applies
        cannot be overtaken by an edit made meanwhile. ``None`` from it writes
        nothing and returns ``None``.
        """
        ...


class SiteEditor(Protocol):
    """Record issuance results against the zone or rule that authored a host."""

    def activate_managed_certificate(
        self,
        site: CdnSite,
        mode: CertificateMode,
        version: CertificateVersion,
        operator: str,
    ) -> CdnSite:
        """Point the zone or rule behind ``site`` at certificate ``version``.

        Returns the host as it is served afterwards.
        """
        ...

    def apply_automatic_ssl_upgrade(
        self, site_name: str, target: SslMode, operator: str
    ) -> CdnSite | None:
        """Move a host enrolled in Automatic SSL up to ``target``.

        Judged on the host as stored at write time, so an operator who opted out or
        chose a stronger mode meanwhile wins; ``None`` when nothing was written.
        """
        ...


class RecordReader(Protocol):
    """Finding a record for a hostname, for preflight's TTL check."""

    def record_for_hostname(self, fqdn: str) -> DnsRecord:
        """Any record answering for ``fqdn``; ``NotFoundError`` if none does."""
        ...


class Issuer(Protocol):
    def issue(self, site: CdnSite, email: str) -> tuple[bytes, bytes]:
        """Obtain a certificate for every name ``site`` serves, registered to ``email``.

        Returns the PEM chain and private key. The caller holds the deployment lock,
        so a challenge never lands mid-convergence. Raises ``ConfigurationError`` for
        a site it cannot issue for and ``ExecutionError`` when issuance fails.
        """
        ...


class CertificateStore(Protocol):
    def install(
        self,
        site: CdnSite,
        certificate_pem: bytes,
        private_key_pem: bytes,
        *,
        source: CertificateSource,
        email: str | None = None,
    ) -> CertificateInfo:
        """Validate and store a chain and key as the site's current certificate.

        The chain must match the key, cover every name ``site`` serves and be valid
        now, or ``ConfigurationError`` is raised. Readers see the whole old pair or
        the whole new pair, never a mix.
        """
        ...

    def get(self, site_name: str) -> CertificateInfo:
        """The site's current certificate; ``NotFoundError`` if it has none."""
        ...

    def list_all(self) -> list[CertificateInfo]:
        """Every certificate held, soonest expiry first, including orphaned ones.

        Unreadable metadata is skipped, so one damaged entry hides nothing else.
        """
        ...

    def collect(self, referenced_paths: Sequence[str]) -> tuple[str, ...]:
        """Delete stored versions no path in ``referenced_paths`` names.

        Returns what was deleted. The caller holds the deployment lock and the write
        transaction; files the store does not recognise are never touched.
        """
        ...

    def sources(
        self, site_name: str, *, version: CertificateVersion | None = None
    ) -> tuple[Path, Path]:
        """The chain and key files of ``version``, or of the current certificate.

        Each is checked against its recorded digest; ``ConfigurationError`` if one is
        missing or altered.
        """
        ...


class Preflight(Protocol):
    def check(
        self, site: CdnSite, *, deployed: bool, record_ttl: int | None = None
    ) -> PreflightReport:
        """Whether HTTP-01 could validate ``site`` right now, without asking the CA.

        ``deployed`` and ``record_ttl`` are stored state the caller reads; the checks
        that need the network are skipped once a blocking DNS failure decides it.
        """
        ...

    def challenge_edges(self, hostname: str) -> ChallengeEdges:
        """The edges the CA could fetch ``hostname``'s HTTP-01 challenge from.

        Every edge when the hostname reaches none that can be seen.
        """
        ...


class PreflightAttempts(Protocol):
    """The last blocked preflight of each site waiting for a certificate."""

    def get(self, site: str) -> PreflightAttempt | None:
        """The last blocked preflight of ``site``, or ``None`` if it has none."""
        ...

    def list_all(self) -> list[PreflightAttempt]:
        """Every recorded blocked preflight, ordered by site."""
        ...

    def record(self, attempt: PreflightAttempt) -> None:
        """Store ``attempt`` as its site's last blocked preflight, replacing any."""
        ...

    def clear(self, site: str) -> None:
        """Forget ``site``'s blocked preflight; a site with none is not an error."""
        ...

    def retain(self, sites: Collection[str]) -> None:
        """Forget every site not named, such as one deleted or now certified."""
        ...


class WorkflowProgress(Protocol):
    def checkpoint(self, name: str, details: dict[str, Any] | None = None) -> object:
        """Record step ``name`` as completed, with ``details`` for recovery review."""
        ...


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
    ) -> WorkflowRun:
        """Open a journalled workflow, closed when the ``with`` block exits.

        An exception leaving the block records failure and propagates.
        """
        ...


__all__ = [
    "CertificateStore",
    "DeploymentGateway",
    "DeploymentLocker",
    "DeploymentRequirements",
    "EventRecorder",
    "IssuanceTimeout",
    "Issuer",
    "MaterialReferences",
    "Preflight",
    "PreflightAttempts",
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

    def inputs(self) -> ReleaseInputs:
        """Canonical desired state as it stands right now."""
        ...

    def inputs_for(self, release_id: str) -> ReleaseInputs:
        """The canonical state the stored release ``release_id`` was compiled from."""
        ...

    def list_releases(self, limit: int | None = 20) -> Sequence[Release]:
        """The ``limit`` most recent releases, newest first; ``None`` for all."""
        ...
