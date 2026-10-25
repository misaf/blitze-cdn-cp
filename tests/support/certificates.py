"""Building this capability's services the way the composition root does.

``control.certificates`` is the service the composition root built. The seams a
test needs (the store, the issuer, the preflight) are substituted by rebuilding
the dataclasses around it. `just types` checks the doubles against their ports.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.capabilities.tls.certificates.domain import (
    CertificateInfo,
    PreflightCheck,
    PreflightReport,
    PreflightSeverity,
)
from blitzecdn.capabilities.tls.certificates.ports import (
    CertificateStore,
    Issuer,
    Preflight,
)
from blitzecdn.capabilities.tls.certificates.service import CertificateService
from blitzecdn.capabilities.tls.contract import SslAutomaticMode, SslMode
from blitzecdn.composition import ApplicationServices, FleetRunner, Repository
from blitzecdn.core.config import Settings
from blitzecdn.core.domain.runs import HostRun
from blitzecdn.core.exceptions import ExecutionError
from blitzecdn.entrypoints.cli import common as cli_common
from support.control_plane import (
    FakeRunner,
    build_application,
    cli_control_plane,
    host_run,
    seed_site,
)

#: What `certificate_pair` hands back: a callable making a certificate and its
#: key. Spelled once because three signatures below take it.
CertificatePair = Callable[..., tuple[bytes, bytes]]


def _with_seams(
    service: CertificateService,
    *,
    certificate_store: CertificateStore | None = None,
    issuer: Issuer | None = None,
    preflight: Preflight | None = None,
) -> CertificateService:
    """``service`` again, with the named collaborators replaced.

    ``CertificateService`` is rebuilt and its two frozen halves ``replace``d.
    Passing nothing returns the service untouched.
    """
    if certificate_store is None and issuer is None and preflight is None:
        return service
    return CertificateService(
        material_references=service.material_references,
        policy=service.policy,
        persistence=replace(
            service.persistence,
            certificates=certificate_store or service.persistence.certificates,
        ),
        execution=replace(
            service.execution,
            issuer=issuer or service.execution.issuer,
            preflight=preflight or service.execution.preflight,
        ),
        events=service.events,
        dns=service.dns,
        # `service.site_editor`, not `service.dns`: the composition root passes the same
        # object to both, but the slot is typed `SiteEditor`.
        site_editor=service.site_editor,
        deployments=service.deployments,
        workflows=service.workflows,
    )


def _attach(
    control: ApplicationServices, service: CertificateService
) -> ApplicationServices:
    """The same graph, with the certificate service's seams replaced."""
    return replace(control, certificates=service)


def certificate_control_plane(
    settings: Settings,
    *,
    runner: FleetRunner | None = None,
    certificate_store: CertificateStore | None = None,
    issuer: Issuer | None = None,
    preflight: Preflight | None = None,
) -> ApplicationServices:
    """A control plane with this capability's services built onto it.

    ``control.certificates`` and ``control.automatic_ssl`` are the composition
    root's, with only the named seams replaced.
    """
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings,
        repository=repository,
        runner=runner if runner is not None else FakeRunner(),
    )

    return _attach(
        control,
        _with_seams(
            control.certificates,
            certificate_store=certificate_store,
            issuer=issuer,
            preflight=preflight,
        ),
    )


def certificate_cli_control_plane(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    runner_double: FleetRunner | None = None,
    preflight: Preflight | None = None,
) -> ApplicationServices:
    """`cli_control_plane`, with this capability's services built onto it.

    `cert list`, `cert renew` and `cert preflight` all read
    `control.certificates`.
    """
    built = cli_control_plane(settings, monkeypatch, runner_double)
    control = _attach(built, _with_seams(built.certificates, preflight=preflight))
    monkeypatch.setattr(cli_common, "services", lambda: control)
    return control


class FakePreflight:
    """Stands in for ``CertificatePreflight`` without touching the network.

    The default is a clean report so issuance tests stay about issuance;
    ``failures`` makes it block, for tests about the refusal.
    """

    def __init__(self, failures: tuple[str, ...] = ()) -> None:
        self.failures = failures
        self.calls: list[tuple[str, bool, int | None]] = []

    def check(
        self, site: CdnSite, *, deployed: bool, record_ttl: int | None = None
    ) -> PreflightReport:
        self.calls.append((site.name, deployed, record_ttl))
        return PreflightReport(
            site=site.name,
            checks=tuple(
                PreflightCheck(
                    name=name,
                    passed=False,
                    severity=PreflightSeverity.BLOCKING,
                    detail=f"{name} failed",
                )
                for name in self.failures
            ),
        )


# --- builders the certificate and Automatic SSL suites share ----------------


def _automatic_origin_report(
    mode: SslMode,
    *,
    reachable: bool = True,
    tls_verified: bool | None = None,
    status: int = 200,
) -> HostRun:
    scheme = "https" if mode in {SslMode.FULL, SslMode.FULL_STRICT} else "http"
    return host_run(
        "edge-a",
        report={
            "host": "edge-a",
            "collected_at": "2026-01-01T00:00:00Z",
            "origins": [
                {
                    "site": "example-com",
                    "origin": f"198.51.100.10:{443 if scheme == 'https' else 80}",
                    "scheme": scheme,
                    "ssl_mode": mode.value,
                    "sni": "198.51.100.10" if scheme == "https" else None,
                    "reachable": str(reachable),
                    "tls_verified": (
                        "None" if tls_verified is None else str(tls_verified)
                    ),
                    "status": str(status) if reachable else "-1",
                    "content_sha256": "a" * 64 if reachable else None,
                    "detail": "",
                }
            ],
        },
    )


def _seed_automatic_ssl_record(
    control: ApplicationServices,
    *,
    mode: SslMode = SslMode.OFF,
    automatic: SslAutomaticMode = SslAutomaticMode.AUTO,
) -> None:
    seed_site(
        control,
        ssl_mode=mode,
        ssl_automatic_mode=automatic,
        certificate_mode="existing",
        certificate_path="/etc/ssl/certs/edge.pem",
        certificate_key_path="/etc/ssl/private/edge.key",
    )


class _RecordingIssuer:
    """Stands in for certbot: hands back a fresh pair and remembers the call."""

    def __init__(
        self, certificate_pair: CertificatePair, *, fails: set[str] | None = None
    ) -> None:
        self._pair = certificate_pair
        self._fails = fails or set()
        self.issued: list[tuple[str, str]] = []

    def issue(self, site: CdnSite, email: str) -> tuple[bytes, bytes]:
        if site.name in self._fails:
            raise ExecutionError("challenge failed")
        self.issued.append((site.name, email))
        return self._pair((site.server_names[0],), days=90)


def _proxied_site_with_certificate(
    control: ApplicationServices,
    repository: Repository,
    certificate_pair: CertificatePair,
    *,
    days: int,
) -> CertificateInfo:
    site = seed_site(control)
    certificate, key = certificate_pair((site.server_names[0],), days=days)
    return control.certificates.upload_certificate(site.name, certificate, key, "alice")


if TYPE_CHECKING:
    # The doubles above stand in for this capability's ports; `just types` holds
    # them to those ports here.
    _preflight_is_a_preflight: Preflight = FakePreflight()
    _issuer_is_an_issuer: Issuer = _RecordingIssuer(lambda *a, **k: (b"", b""))


__all__ = [
    "FakePreflight",
    "_RecordingIssuer",
    "_automatic_origin_report",
    "_proxied_site_with_certificate",
    "_seed_automatic_ssl_record",
    "certificate_cli_control_plane",
    "certificate_control_plane",
]
