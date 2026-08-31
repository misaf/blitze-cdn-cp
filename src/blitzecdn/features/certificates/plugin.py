"""Certificates: issuing them, and telling the edges where they landed.

The desired-state contribution is the interesting one. A site model can say
which mode a host is in, but only this controller knows the fingerprinted file
the material is actually stored under, so the two TLS paths projected from the
model are *overridden* here rather than merged beside them. Saying so in
`overrides` is what makes the merge order-independent: `dns` and `certificates`
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
from blitzecdn.features.certificates import cli, tls_cli
from blitzecdn.features.certificates.api import v1, v2
from blitzecdn.features.dns.site_domain import (
    MANAGED_TLS_ROOT,
    CdnSite,
    CertificateMode,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane

#: The modes whose material this controller holds and must ship to the edges.
#: `EXISTING` points at a file already on the edge and `DISABLED` has none, so
#: neither has a source path to publish.
_CONTROLLER_MANAGED = frozenset({CertificateMode.UPLOADED, CertificateMode.REQUESTED})


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="certificates",
        version=__version__,
        required=True,
        summary="Issue, upload, renew, and publish TLS material.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (v1.router, v2.router)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (
        CliCommandGroup(name="cert", app=cli.cert_app),
        CliCommandGroup(name="ssl", app=tls_cli.ssl_app),
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
        plugin="certificates",
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
    interval = platform.settings.certificate_renewal_interval_seconds
    budget = platform.settings.certificate_renewal_budget_seconds

    def renew(operator: str) -> None:
        platform.certificates.renew_certificates(operator, budget_seconds=budget)

    return (
        ScheduledJob(
            name="certificate-renewal",
            interval_seconds=interval,
            run=renew,
            jitter_seconds=min(3600, interval // 10),
        ),
    )
