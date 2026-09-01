"""Register certificate management and Automatic SSL through Pluggy."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from fastapi import APIRouter

from blitzecdn.core.plugins import (
    CliCommandGroup,
    PluginMetadata,
    ScheduledJob,
    SiteStateContribution,
    hookimpl,
)
from blitzecdn.features.tls.policy import MANAGED_TLS_ROOT, CertificateMode
from blitzecdn_certificates.automatic_ssl import cli as automatic_ssl_cli
from blitzecdn_certificates.automatic_ssl.api import v1 as automatic_ssl_v1
from blitzecdn_certificates.automatic_ssl.api import v2 as automatic_ssl_v2
from blitzecdn_certificates.certificates import cli as certificates_cli
from blitzecdn_certificates.certificates.api import v1 as certificates_v1
from blitzecdn_certificates.certificates.api import v2 as certificates_v2
from blitzecdn_certificates.composition import (
    __version__,
    build_automatic_ssl_service,
    build_certificate_service,
)

if TYPE_CHECKING:
    from blitzecdn.bootstrap import ControlPlane
    from blitzecdn.features.sites.domain import CdnSite

_CONTROLLER_MANAGED = frozenset({CertificateMode.UPLOADED, CertificateMode.REQUESTED})


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="certificates",
        version=__version__,
        required=False,
        provides=frozenset({"certificates"}),
        summary="Certificate upload, ACME renewal, and Automatic SSL/TLS.",
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
    certificate, private_key = build_certificate_service(platform).installed_sources(
        site.name
    )
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
    certificates = build_certificate_service(platform)
    automatic_ssl = build_automatic_ssl_service(platform)
    renewal_interval = platform.settings.certificate_renewal_interval_seconds
    budget = platform.settings.certificate_renewal_budget_seconds
    scan_interval = platform.settings.ssl_automatic_scan_interval_seconds
    reconcile_interval = platform.settings.certificate_reconcile_interval_seconds

    def renew(operator: str) -> None:
        certificates.renew_certificates(operator, budget_seconds=budget)

    def scan(operator: str) -> None:
        automatic_ssl.reconcile(operator)

    def reconcile(operator: str) -> None:
        if certificates.reconcile_certificates(operator).issued:
            automatic_ssl.reconcile(operator)

    return (
        ScheduledJob(
            name="certificate-reconciliation",
            interval_seconds=reconcile_interval,
            run=reconcile,
        ),
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
