"""How certificate management is built, and what it renders and runs.

Rendering reads verified paths from the certificate store, never the service,
which is built after the release compiler.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from blitzecdn.capabilities.edges.ports import EdgeStore, OriginProbe
from blitzecdn.capabilities.tls.automatic_ssl.ports import OriginCheckRunner
from blitzecdn.capabilities.tls.automatic_ssl.service import AutomaticSslService
from blitzecdn.capabilities.tls.certificates.adapters import CertbotIssuer
from blitzecdn.capabilities.tls.certificates.adapters.preflight import (
    CertificatePreflight,
)
from blitzecdn.capabilities.tls.certificates.ports import (
    CertificateStore,
    DeploymentGateway,
    DeploymentLocker,
    DeploymentRequirements,
    EventRecorder,
    RecordReader,
    ReleaseHistory,
    SiteEditor,
    SiteReader,
    SiteWriter,
    UnitOfWork,
    WorkflowCoordinator,
)
from blitzecdn.capabilities.tls.certificates.service import (
    CertificateExecution,
    CertificatePersistence,
    CertificatePolicy,
    CertificateService,
    SiteEditing,
)
from blitzecdn.capabilities.tls.contract import (
    CertificateMode,
    CertificateVersion,
    TlsPolicy,
)
from blitzecdn.capabilities.tls.settings import CertificateConfig
from blitzecdn.core.config import Settings
from blitzecdn.core.features import (
    FeatureOperations,
    FeatureRendering,
    ScheduledJob,
    Severity,
    SiteStateContribution,
    ValidationIssue,
)
from blitzecdn.core.ports.clock import Clock, system_clock
from blitzecdn.core.runtime.resources import distribution_version

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.capabilities.serving.domain import CdnSite


#: The installed distribution's version, read from metadata.
__version__ = distribution_version(__name__)


class CertificateMaterialReferences:
    """All authored TLS paths and every retained release's canonical inputs."""

    def __init__(self, releases: ReleaseHistory) -> None:
        self._releases = releases

    def __call__(self) -> tuple[str, ...]:
        inputs = [self._releases.inputs()]
        inputs.extend(
            self._releases.inputs_for(release.id)
            for release in self._releases.list_releases(limit=None)
        )
        paths: set[str] = set()
        for state in inputs:
            for zone in state.domains:
                paths.update(
                    path
                    for path in (
                        zone.policy[TlsPolicy].certificate_path,
                        zone.policy[TlsPolicy].certificate_key_path,
                    )
                    if path is not None
                )
            for rule in state.rules:
                for name in ("certificate_path", "certificate_key_path"):
                    path = rule.overrides.get(name)
                    if isinstance(path, str):
                        paths.add(path)
        return tuple(sorted(paths))


def build_site_editor(*, writer: SiteWriter) -> SiteEditor:
    """Wire the TLS writes onto the zone or rule behind a host."""
    return SiteEditing(writer=writer)


def build_certificate_service(
    *,
    settings: Settings,
    config: CertificateConfig,
    store: CertificateStore,
    releases: ReleaseHistory,
    sites: SiteReader,
    edges: EdgeStore,
    origin_probe: OriginProbe,
    requirements: DeploymentRequirements,
    lock: DeploymentLocker,
    events: EventRecorder,
    dns: RecordReader,
    site_editor: SiteEditor,
    deployments: DeploymentGateway,
    workflows: WorkflowCoordinator,
    uow: UnitOfWork,
    clock: Clock = system_clock,
) -> CertificateService:
    """Wire issuance, collection and reconciliation of managed certificates."""
    return CertificateService(
        material_references=CertificateMaterialReferences(releases),
        policy=CertificatePolicy(default_email=config.default_email),
        persistence=CertificatePersistence(
            sites=sites,
            certificates=store,
            uow=uow,
            requirements=requirements,
        ),
        execution=CertificateExecution(
            runner=lock,
            issuer=CertbotIssuer(settings, config.certbot),
            preflight=CertificatePreflight(
                settings,
                edges,
                ca_domain=config.ca_domain,
                origin_probe=origin_probe,
            ),
        ),
        events=events,
        dns=dns,
        site_editor=site_editor,
        deployments=deployments,
        workflows=workflows,
        clock=clock,
    )


def build_automatic_ssl_service(
    *,
    sites: SiteReader,
    origin_checks: OriginCheckRunner,
    origin_probe: OriginProbe,
    site_editor: SiteEditor,
    deployments: DeploymentGateway,
) -> AutomaticSslService:
    """Wire upgrades that apply only what both origin transports prove."""
    return AutomaticSslService(
        sites=sites,
        runner=origin_checks,
        origin_probe=origin_probe,
        site_editor=site_editor,
        deployments=deployments,
    )


#: Names no public CA will issue for (RFC 6761/2606).
_RESERVED_SUFFIXES = (".test", ".invalid", ".localhost", ".example")


def unissuable_names(site: CdnSite) -> Sequence[ValidationIssue]:
    """Refuse a site whose ACME request can never be answered."""
    if (
        not site.policy.enabled
        or site.policy[TlsPolicy].certificate_mode is not CertificateMode.REQUESTED
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


def certificates_rendering(*, store: CertificateStore) -> FeatureRendering[CdnSite]:
    """Override a site's certificate paths with the verified installed files."""

    def site_state(site: CdnSite) -> SiteStateContribution | None:
        if not site.policy[TlsPolicy].certificate_mode.issuer_owned:
            return None
        version = CertificateVersion.from_paths(
            site.name,
            site.policy[TlsPolicy].certificate_path or "",
            site.policy[TlsPolicy].certificate_key_path or "",
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
                # The one job with its own lease: a sweep is bounded by its budget.
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
