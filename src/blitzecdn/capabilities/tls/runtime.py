"""What certificate management renders and runs.

Rendering reads verified source paths from the certificate store, never from
the certificate service: the release compiler is built before that service,
and what a site renders to must not depend on what a service holds.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from blitzecdn.capabilities.tls.automatic_ssl.service import AutomaticSslService
from blitzecdn.capabilities.tls.certificates.ports import CertificateStore
from blitzecdn.capabilities.tls.certificates.service import CertificateService
from blitzecdn.capabilities.tls.config import CertificateConfig
from blitzecdn.capabilities.tls.policy import CertificateMode, CertificateVersion
from blitzecdn.core.features import (
    FeatureOperations,
    FeatureRendering,
    ScheduledJob,
    Severity,
    SiteStateContribution,
    ValidationIssue,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.capabilities.serving.domain import CdnSite

#: Names no public CA will issue for (RFC 6761/2606).
_RESERVED_SUFFIXES = (".test", ".invalid", ".localhost", ".example")


def unissuable_names(site: CdnSite) -> Sequence[ValidationIssue]:
    """Refuse a site whose ACME request can never be answered.

    What a public CA will issue for is knowledge of the capability that asks
    one, so it belongs here rather than in the zone editor's validation.
    """
    if (
        not site.policy.enabled
        or site.policy.tls.certificate_mode is not CertificateMode.REQUESTED
    ):
        return ()
    return tuple(
        ValidationIssue(
            feature="certificates",
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


def certificates_rendering(*, store: CertificateStore) -> FeatureRendering:
    """Override a site's certificate paths with the verified installed files."""

    def site_state(site: CdnSite) -> SiteStateContribution | None:
        if not site.policy.tls.certificate_mode.issuer_owned:
            return None
        version = CertificateVersion.from_paths(
            site.name,
            site.policy.tls.certificate_path or "",
            site.policy.tls.certificate_key_path or "",
        )
        certificate, private_key = store.sources(site.name, version=version)
        return SiteStateContribution(
            feature="certificates",
            variables={
                "certificate_source_path": str(certificate),
                "certificate_key_source_path": str(private_key),
                "certificate_source_sha256": version.certificate_digest,
                "certificate_key_source_sha256": version.private_key_digest,
            },
        )

    return FeatureRendering(
        feature="certificates",
        site_state=site_state,
        deployment_checks=unissuable_names,
    )


def certificates_operations(
    *,
    certificates: CertificateService,
    automatic_ssl: AutomaticSslService,
    config: CertificateConfig,
    deployment_timeout_seconds: int,
) -> FeatureOperations:
    """Renewal, reconciliation and the Automatic SSL/TLS scan."""
    budget = config.renewal_budget_seconds

    def renew(operator: str) -> None:
        certificates.renew_certificates(operator, budget_seconds=budget)

    def scan(operator: str) -> None:
        automatic_ssl.reconcile(operator)

    def reconcile(operator: str) -> None:
        if certificates.reconcile_certificates(operator).issued:
            automatic_ssl.reconcile(operator)

    return FeatureOperations(
        feature="certificates",
        scheduled_jobs=(
            ScheduledJob(
                feature="certificates",
                name="certificate-reconciliation",
                interval_seconds=config.reconcile_interval_seconds,
                run=reconcile,
            ),
            ScheduledJob(
                feature="certificates",
                name="certificate-renewal",
                interval_seconds=config.renewal_interval_seconds,
                run=renew,
                jitter_seconds=min(3600, config.renewal_interval_seconds // 10),
                # The one job that declares its own lease. A sweep is bounded
                # by its budget rather than by its cadence, so "twice the
                # interval" — every other job's default — is not its floor.
                lease_seconds=deployment_timeout_seconds + budget,
            ),
            ScheduledJob(
                feature="certificates",
                name="automatic-ssl-scan",
                interval_seconds=config.scan_interval_seconds,
                run=scan,
                jitter_seconds=min(86_400, config.scan_interval_seconds // 30),
            ),
        ),
    )
