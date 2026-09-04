"""Building this capability's services the way the composition root does.

The control plane's shared fixtures used to carry an
``attach_certificate_test_services`` fixture that monkeypatched
``ControlPlane.__init__`` so that ``control.certificates`` existed and could be
handed a fake store, issuer or preflight. Its own docstring called it legacy,
and it was: production core never constructs these services, and every test
that relied on it was a certificates test living in ``tests/``.

Here the wiring is the real one. ``build_certificate_service`` is the same
function the plugin's registration calls, and the seams a test needs — the
store, the issuer, the preflight — are substituted by rebuilding the two
dataclasses around it rather than by intercepting a constructor.
"""

from __future__ import annotations

from dataclasses import replace

from blitzecdn_certificates.certificates.domain import (
    PreflightCheck,
    PreflightReport,
    PreflightSeverity,
)
from blitzecdn_certificates.certificates.service import CertificateService
from blitzecdn_certificates.composition import (
    _certificate_services,
    build_automatic_ssl_service,
    build_certificate_service,
)
from control_plane_fixtures import (
    FakeRunner,
    cli_control_plane,
    host_run,
    seed_site,
)

from blitzecdn.bootstrap import ControlPlane
from blitzecdn.capabilities.tls.policy import SslAutomaticMode, SslMode
from blitzecdn.core.domain.runs import HostRun
from blitzecdn.core.exceptions import ExecutionError
from blitzecdn.persistence import Repository


def _with_seams(service, *, certificate_store=None, issuer=None, preflight=None):
    """``service`` again, with the named collaborators replaced.

    ``CertificateService`` is a plain class, so it is rebuilt; the two halves it
    holds are frozen dataclasses, so those are ``replace``d. Passing nothing
    returns the service untouched, which keeps the default path the one the
    composition root produced.
    """
    if certificate_store is None and issuer is None and preflight is None:
        return service
    return CertificateService(
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
        site_editor=service.site_editor,
        deployments=service.deployments,
        workflows=service.workflows,
    )


def _attach(control, service):
    """Publish ``service`` where the routes, jobs and sibling services look.

    ``_certificate_services`` is the composition root's per-control-plane cache;
    a substituted service has to be the one it hands back, or the API routes
    and the Automatic SSL builder would each find the unsubstituted one.
    """
    _certificate_services[control] = service
    control.certificates = service
    control.automatic_ssl = build_automatic_ssl_service(control)
    return control


def certificate_control_plane(
    settings,
    *,
    runner=None,
    certificate_store=None,
    issuer=None,
    preflight=None,
):
    """A control plane with this capability's services built onto it.

    Returns the control plane. ``control.certificates`` and
    ``control.automatic_ssl`` are set because the services under test read each
    other through the platform object, exactly as the plugin's own startup hook
    arranges — but they are the services the composition root builds, with only
    the named seams replaced.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=runner if runner is not None else FakeRunner(),
    )  # type: ignore[arg-type]

    return _attach(
        control,
        _with_seams(
            build_certificate_service(control),
            certificate_store=certificate_store,
            issuer=issuer,
            preflight=preflight,
        ),
    )


def certificate_cli_control_plane(
    settings, monkeypatch, runner_double=None, preflight=None
):
    """`cli_control_plane`, with this capability's services built onto it.

    The command tree an operator sees is assembled from every installed plugin,
    so core's helper is what points the CLI at a control plane. It builds no
    certificate services — core has none to build — and `cert list`, `cert
    renew` and `cert preflight` all read `control.certificates`.
    """
    control = cli_control_plane(settings, monkeypatch, runner_double)
    return _attach(
        control,
        _with_seams(build_certificate_service(control), preflight=preflight),
    )


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

    def check(self, site, *, deployed: bool, record_ttl: int | None = None):
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
# These were in `tests/application_support.py`, where core's own suites could
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
                    "site": "cdn-example-com",
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
    control: ControlPlane,
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

    def __init__(self, certificate_pair, *, fails: set[str] | None = None) -> None:
        self._pair = certificate_pair
        self._fails = fails or set()
        self.issued: list[tuple[str, str]] = []

    def issue(self, site, email):
        if site.name in self._fails:
            raise ExecutionError("challenge failed")
        self.issued.append((site.name, email))
        return self._pair((site.server_names[0],), days=90)


def _proxied_site_with_certificate(control, repository, certificate_pair, *, days):
    site = seed_site(control)
    certificate, key = certificate_pair((site.server_names[0],), days=days)
    return control.certificates.upload_certificate(site.name, certificate, key, "alice")


__all__ = [
    "FakePreflight",
    "_RecordingIssuer",
    "_automatic_origin_report",
    "_proxied_site_with_certificate",
    "_seed_automatic_ssl_record",
    "certificate_cli_control_plane",
    "certificate_control_plane",
]
