"""How certificate management is built, and what it renders and runs.

Rendering reads verified paths from the certificate store, never the service,
which is built after the release compiler.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from dishka import Provider, Scope, alias, provide

from blitzecdn.capabilities.edges import EdgeStore, OriginProbe
from blitzecdn.capabilities.tls.adapters.persistence import PreflightAttemptStore
from blitzecdn.capabilities.tls.certificates.adapters import CertbotIssuer
from blitzecdn.capabilities.tls.certificates.adapters import (
    CertificateStore as CertificateFiles,
)
from blitzecdn.capabilities.tls.certificates.adapters.preflight import (
    CertificatePreflight,
)
from blitzecdn.capabilities.tls.certificates.domain import PreflightBackoff
from blitzecdn.capabilities.tls.certificates.ports import (
    CertbotTimeout,
    CertificateStore,
    DeploymentGateway,
    DeploymentLocker,
    DeploymentRequirements,
    EventRecorder,
    PreflightAttempts,
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
from blitzecdn.capabilities.tls.ports import CertificateHistory
from blitzecdn.capabilities.tls.settings import CONFIGURATION, CertificateConfig
from blitzecdn.core.config import Settings
from blitzecdn.core.features import (
    FeatureOperations,
    FeatureRendering,
    ScheduledJob,
    Severity,
    SiteStateContribution,
    ValidationIssue,
)
from blitzecdn.core.features.resolution import ResolvedCapabilityEnvironment
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.ports.clock import Clock
from blitzecdn.core.runtime.resources import distribution_version

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.core.domain.site import CdnSite


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
    preflight_attempts: PreflightAttempts,
    certbot_timeout_seconds: int,
    clock: Clock,
) -> CertificateService:
    """Wire issuance, collection and reconciliation of managed certificates."""
    return CertificateService(
        material_references=CertificateMaterialReferences(releases),
        policy=CertificatePolicy(
            default_email=config.default_email,
            # Starts at the reconciliation interval; a disabled timer still
            # needs a sensible base for runs an operator starts by hand.
            preflight_backoff=PreflightBackoff(
                interval_seconds=config.reconcile_interval_seconds or 600
            ),
        ),
        persistence=CertificatePersistence(
            sites=sites,
            certificates=store,
            uow=uow,
            requirements=requirements,
            preflight_attempts=preflight_attempts,
        ),
        execution=CertificateExecution(
            runner=lock,
            issuer=CertbotIssuer(
                settings, config.certbot, timeout_seconds=certbot_timeout_seconds
            ),
            preflight=CertificatePreflight(config, edges, origin_probe=origin_probe),
        ),
        events=events,
        dns=dns,
        site_editor=site_editor,
        deployments=deployments,
        workflows=workflows,
        clock=clock,
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
    config: CertificateConfig,
    deployment_timeout_seconds: int,
    after_issuance: Callable[[str], object] | None = None,
) -> FeatureOperations:
    """Renewal and reconciliation.

    ``after_issuance`` runs once a reconciliation pass has issued anything; the
    composition root hands it whatever reacts to new certificates (the
    Automatic SSL/TLS scan), so TLS never names what builds on it.
    """
    budget = config.renewal_budget_seconds

    def renew(operator: str) -> None:
        certificates.renew_certificates(operator, budget_seconds=budget)

    def reconcile(operator: str) -> None:
        issued = certificates.reconcile_certificates(operator).issued
        if issued and after_issuance is not None:
            after_issuance(operator)

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
        ),
    )


class TlsProvider(Provider):
    """Certificate files and preflight records, and issuance."""

    scope = Scope.APP

    @provide
    def config(
        self, capability_config: ResolvedCapabilityEnvironment
    ) -> CertificateConfig:
        return capability_config.for_feature(CONFIGURATION.feature).as_model(
            CertificateConfig
        )

    @provide
    def files(self, settings: Settings, clock: Clock) -> CertificateFiles:
        return CertificateFiles(settings, clock=clock)

    certificate_store = alias(source=CertificateFiles, provides=CertificateStore)

    @provide
    def preflight_attempts(self, database: Database) -> PreflightAttempts:
        return PreflightAttemptStore(database)

    @provide
    def site_editor(self, writer: SiteWriter) -> SiteEditor:
        # DNS writes where a host's policy comes from; TLS decides what it writes.
        return build_site_editor(writer=writer)

    @provide
    def certificates(
        self,
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
        preflight_attempts: PreflightAttempts,
        certbot_timeout: CertbotTimeout,
        clock: Clock,
    ) -> CertificateService:
        return build_certificate_service(
            settings=settings,
            config=config,
            store=store,
            releases=releases,
            sites=sites,
            edges=edges,
            origin_probe=origin_probe,
            requirements=requirements,
            lock=lock,
            events=events,
            dns=dns,
            site_editor=site_editor,
            deployments=deployments,
            workflows=workflows,
            uow=uow,
            preflight_attempts=preflight_attempts,
            certbot_timeout_seconds=certbot_timeout,
            clock=clock,
        )

    history = alias(source=CertificateService, provides=CertificateHistory)
