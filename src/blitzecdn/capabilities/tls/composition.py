"""Wire certificate operations from public control-plane ports."""

from __future__ import annotations

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
    UnitOfWork,
    WorkflowCoordinator,
)
from blitzecdn.capabilities.tls.certificates.service import (
    CertificateExecution,
    CertificatePersistence,
    CertificatePolicy,
    CertificateService,
)
from blitzecdn.capabilities.tls.config import CertificateConfig
from blitzecdn.core.config import Settings
from blitzecdn.core.runtime.resources import distribution_version

#: This distribution's version, asked of the environment rather than
#: written down here: it is what ``PluginMetadata.version`` reports and
#: what ``blitzecdn plugins`` shows an operator, so the one number that
#: must not drift from ``pyproject.toml`` is not copied out of it.
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
                        zone.policy.tls.certificate_path,
                        zone.policy.tls.certificate_key_path,
                    )
                    if path is not None
                )
            for rule in state.rules:
                for name in ("certificate_path", "certificate_key_path"):
                    path = rule.overrides.get(name)
                    if isinstance(path, str):
                        paths.add(path)
        return tuple(sorted(paths))


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
