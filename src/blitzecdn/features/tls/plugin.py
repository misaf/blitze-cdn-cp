"""Register the TLS capability: policy, certificates, and Automatic SSL/TLS.

One registration for one capability, replacing the two that used to sit beside
each other. The contributions themselves did not merge — a router is still a
router and a job is still a job — but they are declared in one place now, which
is what makes "who owns SslMode" answerable.

The desired-state contribution is the interesting one. A site model can say
which mode a host is in, but only this controller knows the fingerprinted file
the material is actually stored under, so the two TLS paths projected from the
site model are *overridden* here rather than merged beside them. Saying so in
``overrides`` is what makes the merge order-independent: ``sites`` and ``tls``
can register in either order and the edge converges identically.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.core.plugins import (
    CliCommandGroup,
    PluginMetadata,
    ScheduledJob,
    SiteStateContribution,
    hookimpl,
)
from blitzecdn.features.tls.automatic_ssl import cli as automatic_ssl_cli
from blitzecdn.features.tls.automatic_ssl.api import v1 as automatic_ssl_v1
from blitzecdn.features.tls.automatic_ssl.api import v2 as automatic_ssl_v2
from blitzecdn.features.tls.certificates import cli as certificates_cli
from blitzecdn.features.tls.certificates.api import v1 as certificates_v1
from blitzecdn.features.tls.certificates.api import v2 as certificates_v2
from blitzecdn.features.tls.policy import MANAGED_TLS_ROOT, CertificateMode

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane
    from blitzecdn.features.sites.domain import CdnSite

#: The modes whose material this controller holds and must ship to the edges.
#: `EXISTING` points at a file already on the edge and `DISABLED` has none, so
#: neither has a source path to publish.
_CONTROLLER_MANAGED = frozenset({CertificateMode.UPLOADED, CertificateMode.REQUESTED})


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="tls",
        version=__version__,
        required=True,
        summary=(
            "Edge encryption: TLS policy, certificate material, and the "
            "upgrade-only Automatic SSL/TLS scan."
        ),
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (
        certificates_v1.router,
        certificates_v2.router,
        automatic_ssl_v1.router,
        automatic_ssl_v2.router,
    )


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (
        CliCommandGroup(name="cert", app=certificates_cli.cert_app),
        CliCommandGroup(name="ssl", app=automatic_ssl_cli.ssl_app),
    )


@hookimpl
def blitzecdn_site_desired_state(
    site: CdnSite, platform: ControlPlane
) -> SiteStateContribution | None:
    if site.certificate_mode not in _CONTROLLER_MANAGED:
        return None
    certificate, private_key = platform.certificates.installed_sources(site.name)
    destination = PurePosixPath(MANAGED_TLS_ROOT, site.name)
    return SiteStateContribution(
        plugin="tls",
        variables={
            "certificate_source_path": str(certificate),
            "certificate_key_source_path": str(private_key),
            "certificate_path": str(destination / certificate.name),
            "certificate_key_path": str(destination / private_key.name),
        },
        overrides=frozenset({"certificate_path", "certificate_key_path"}),
    )


@hookimpl
def blitzecdn_scheduled_jobs(platform: ControlPlane) -> Sequence[ScheduledJob]:
    """Renewal and the Automatic SSL/TLS scan, the capability's two timers."""
    renewal_interval = platform.settings.certificate_renewal_interval_seconds
    budget = platform.settings.certificate_renewal_budget_seconds
    scan_interval = platform.settings.ssl_automatic_scan_interval_seconds

    def renew(operator: str) -> None:
        platform.certificates.renew_certificates(operator, budget_seconds=budget)

    def scan(operator: str) -> None:
        # The reconciliation report is for the caller who asked for one; a
        # scheduled run has no caller, and the work it did is already in the
        # audit trail.
        platform.automatic_ssl.reconcile(operator)

    return (
        ScheduledJob(
            name="certificate-renewal",
            interval_seconds=renewal_interval,
            run=renew,
            jitter_seconds=min(3600, renewal_interval // 10),
        ),
        ScheduledJob(
            name="automatic-ssl-scan",
            interval_seconds=scan_interval,
            run=scan,
            jitter_seconds=min(86_400, scan_interval // 30),
        ),
    )
