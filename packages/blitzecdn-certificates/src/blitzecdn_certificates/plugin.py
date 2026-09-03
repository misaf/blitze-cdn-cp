"""Register certificate management and Automatic SSL through Pluggy."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from fastapi import APIRouter

from blitzecdn.core.plugins import (
    CliCommandGroup,
    ConfigurationContribution,
    PluginMetadata,
    ScheduledJob,
    Severity,
    SiteStateContribution,
    ValidationIssue,
    hookimpl,
)
from blitzecdn.capabilities.tls.policy import MANAGED_TLS_ROOT, CertificateMode
from blitzecdn_certificates.automatic_ssl import cli as automatic_ssl_cli
from blitzecdn_certificates.automatic_ssl.api import routes as automatic_ssl_routes
from blitzecdn_certificates.certificates import cli as certificates_cli
from blitzecdn_certificates.certificates.api import routes as certificates_routes
from blitzecdn_certificates.composition import (
    __version__,
    build_automatic_ssl_service,
    build_certificate_service,
    certificate_config,
)
from blitzecdn_certificates.config import SETTINGS

if TYPE_CHECKING:
    from blitzecdn.bootstrap import ControlPlane
    from blitzecdn.capabilities.sites.domain import CdnSite

_CONTROLLER_MANAGED = frozenset({CertificateMode.UPLOADED, CertificateMode.REQUESTED})

#: Names no public CA will issue for (RFC 6761/2606).
_RESERVED_SUFFIXES = (".test", ".invalid", ".localhost", ".example")


@hookimpl
def blitzecdn_deployment_checks(
    site: CdnSite, platform: ControlPlane
) -> Sequence[ValidationIssue]:
    """Refuse a site whose ACME request can never be answered.

    This check used to live in the zone editor's ``validation_errors``, back
    when a site was derived from a record and the reserved suffix was read off
    the record's zone. It reads the site's own hostnames now — and it belongs
    here rather than in core either way: what a public CA will issue for is
    knowledge of the capability that asks one, and an installation without this
    distribution cannot reach ``certificate_mode='requested'`` at all.
    """
    if not site.enabled or site.certificate_mode is not CertificateMode.REQUESTED:
        return ()
    return tuple(
        ValidationIssue(
            plugin="certificates",
            site=site.name,
            severity=Severity.BLOCKING,
            message=(
                f"an ACME certificate is requested for {server_name!r}, a "
                "reserved name (RFC 6761/2606) that no public CA will issue "
                "for. Upload a certificate instead."
            ),
        )
        for server_name in site.server_names
        if server_name.endswith(_RESERVED_SUFFIXES)
    )


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
def blitzecdn_capability_configuration() -> Sequence[ConfigurationContribution]:
    """Claim this capability's seven controller-side names.

    None of them is a secret, which is why they were the last configuration
    still living on core's ``Settings``: an ``EnvironmentKey`` was the only
    thing a capability could declare, and none of these is one.
    """
    return (ConfigurationContribution(plugin="certificates", settings=SETTINGS),)


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (certificates_routes.router, automatic_ssl_routes.router)


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
    config = certificate_config(platform)
    renewal_interval = config.renewal_interval_seconds
    budget = config.renewal_budget_seconds
    scan_interval = config.scan_interval_seconds
    reconcile_interval = config.reconcile_interval_seconds

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
            # The one job that declares its own lease. A sweep is bounded by
            # its budget rather than by its cadence, so "twice the interval"
            # — the default every other job takes — is not the right floor
            # for it. The scheduler used to apply this capability's budget to
            # every job in the installation to get the same effect.
            lease_seconds=platform.settings.deployment_timeout_seconds + budget,
        ),
        ScheduledJob(
            name="automatic-ssl-scan",
            interval_seconds=scan_interval,
            run=scan,
            jitter_seconds=min(86_400, scan_interval // 30),
        ),
    )
