"""Wire certificate operations from public control-plane ports."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from blitzecdn.capabilities.tls.automatic_ssl.service import AutomaticSslService
from blitzecdn.capabilities.tls.certificates.adapters import (
    CertbotIssuer,
    CertificateStore,
)
from blitzecdn.capabilities.tls.certificates.adapters.preflight import (
    CertificatePreflight,
)
from blitzecdn.capabilities.tls.certificates.ports import WorkflowCoordinator
from blitzecdn.capabilities.tls.certificates.service import (
    CertificateExecution,
    CertificatePersistence,
    CertificatePolicy,
    CertificateService,
)
from blitzecdn.core.runtime.resources import distribution_version

if TYPE_CHECKING:
    from blitzecdn.composition import ControlPlane

#: This distribution's version, asked of the environment rather than
#: written down here: it is what ``PluginMetadata.version`` reports and
#: what ``blitzecdn plugins`` shows an operator, so the one number that
#: must not drift from ``pyproject.toml`` is not copied out of it.
__version__ = distribution_version(__name__)


class CertificateMaterialReferences:
    """All authored TLS paths and every retained release's canonical inputs."""

    def __init__(self, platform: ControlPlane) -> None:
        self.platform = platform

    def __call__(self) -> tuple[str, ...]:
        inputs = [self.platform.releases.inputs()]
        inputs.extend(
            self.platform.releases.inputs_for(release.id)
            for release in self.platform.releases.list_releases(limit=None)
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


def build_certificate_service(platform: ControlPlane) -> CertificateService:
    config = platform.certificate_config
    return CertificateService(
        material_references=CertificateMaterialReferences(platform),
        policy=CertificatePolicy(default_email=config.default_email),
        persistence=CertificatePersistence(
            sites=platform.sites,
            certificates=CertificateStore(platform.settings),
            uow=platform.transactions,
            requirements=platform.deployment_requirements,
        ),
        execution=CertificateExecution(
            runner=platform.deployment_lock,
            issuer=CertbotIssuer(platform.settings, config.certbot),
            preflight=CertificatePreflight(
                platform.settings,
                platform.edge_inventory,
                ca_domain=config.ca_domain,
                origin_probe=platform.origin_probe,
            ),
        ),
        events=platform.events,
        dns=platform.dns,
        site_editor=platform.site_editor,
        deployments=platform.deployments,
        # ``@contextmanager`` exposes its concrete private context-manager type
        # to mypy. The public behavior is the narrow protocol the service uses.
        workflows=cast(WorkflowCoordinator, platform.workflows),
    )


def build_automatic_ssl_service(platform: ControlPlane) -> AutomaticSslService:
    return AutomaticSslService(
        sites=platform.sites,
        # The application root supplies the origins adapter through its port.
        runner=platform.origin_checks,
        origin_probe=platform.origin_probe,
        site_editor=platform.site_editor,
        deployments=platform.deployments,
    )
