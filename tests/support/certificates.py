"""Building this capability's services the way the composition root does.

The control plane's shared fixtures used to carry an
``attach_certificate_test_services`` fixture that monkeypatched
``ApplicationServices.__init__`` so that ``control.certificates`` existed and could be
handed a fake store, issuer or preflight. Its own docstring called it legacy,
and it was: production core never constructs these services, and every test
that relied on it was a certificates test living in ``tests/``.

Here the wiring is the real one. ``control.certificates`` is the service the
composition root built, and the seams a test needs — the store, the issuer, the
preflight — are substituted by rebuilding the two dataclasses around it rather
than by intercepting a constructor.

`just types` checks this module, which is what makes the seams above claims
rather than hopes. ``FakePreflight`` and ``_RecordingIssuer`` are handed
straight to ``CertificateExecution``, whose fields are the ``Preflight`` and
``Issuer`` protocols; before the gate reached here, a double that had drifted
from its port would have been caught by whichever test happened to call the
method that moved, and reported as that test failing.
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

    ``CertificateService`` is a plain class, so it is rebuilt; the two halves it
    holds are frozen dataclasses, so those are ``replace``d. Passing nothing
    returns the service untouched, which keeps the default path the one the
    composition root produced.
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
        # `service.site_editor`, not `service.dns` a second time. The composition
        # root hands the same object to both, so reconstructing this slot from
        # `dns` was right by coincidence — and `just types` reaching this file
        # is what said so: `dns` is a `RecordReader` and the slot wants a
        # `SiteEditor`. Preserving what the service already holds is what
        # "again, with the named collaborators replaced" claims to do.
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

    Returns the control plane. ``control.certificates`` and
    ``control.automatic_ssl`` are set because the services under test read each
    other through the application composition root, with only
    the named seams replaced.
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

    The command tree an operator sees is assembled from every installed feature,
    so core's helper is what points the CLI at a control plane. It builds no
    additional services; `cert list`, `cert renew` and `cert preflight` all read
    `control.certificates`.
    """
    built = cli_control_plane(settings, monkeypatch, runner_double)
    control = _attach(built, _with_seams(built.certificates, preflight=preflight))
    monkeypatch.setattr(cli_common, "services", lambda: control)
    return control


class FakePreflight:
    """Stands in for ``CertificatePreflight`` without touching the network.

    Certificate preflight resolves hostnames, queries CAA and probes origins,
    none of which a test can do. The default is a clean report so tests about
    issuance stay about issuance; ``failures`` makes it block, for the tests
    that are about the refusal itself.

    It was one of core's shared fixtures, where its ``check`` had to reach the
    report types through ``import_module`` because core cannot import an
    optional distribution. Beside the types it builds, it is an ordinary class.
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
#
# These were in `support/application.py`, where core's own suites could
# reach them. Nothing in core uses them any more: an issuer stub, an uploaded
# certificate and an origin report shaped for the Automatic SSL scan are all
# this capability's vocabulary.


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
    # The doubles above stand in for this capability's ports, stated here and
    # held by `just types` — the same arrangement `support/control_plane.py`
    # uses for core's. `CertificateExecution` takes both, so a drift used to
    # surface as whichever test called the method that moved.
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
