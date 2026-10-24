"""Wire certificate operations from public control-plane ports."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from weakref import WeakKeyDictionary

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
from blitzecdn.capabilities.tls.config import CertificateConfig
from blitzecdn.core.runtime.resources import distribution_version

if TYPE_CHECKING:
    from blitzecdn.composition import ControlPlane

#: This distribution's version, asked of the environment rather than
#: written down here: it is what ``PluginMetadata.version`` reports and
#: what ``blitzecdn plugins`` shows an operator, so the one number that
#: must not drift from ``pyproject.toml`` is not copied out of it.
__version__ = distribution_version(__name__)

_configs: WeakKeyDictionary[object, CertificateConfig] = WeakKeyDictionary()
_certificate_services: WeakKeyDictionary[object, CertificateService] = (
    WeakKeyDictionary()
)
_automatic_ssl_services: WeakKeyDictionary[object, AutomaticSslService] = (
    WeakKeyDictionary()
)


def certificate_config(platform: ControlPlane) -> CertificateConfig:
    """This capability's own configuration, resolved once per control plane.

    Cached like the services are, and for the same reason: it is read by the
    scheduled jobs, by the renewal route and by two adapters, and re-deriving
    it would mean the CA-identity refusal fired in whichever of them happened
    to run first rather than at composition.
    """
    existing = _configs.get(platform)
    if existing is not None:
        return existing
    config = CertificateConfig.from_capability_config(
        platform.capability_config.for_plugin("certificates")
    )
    _configs[platform] = config
    return config


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
    existing = _certificate_services.get(platform)
    if existing is not None:
        return existing
    config = certificate_config(platform)
    service = CertificateService(
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
    _certificate_services[platform] = service
    return service


def build_automatic_ssl_service(platform: ControlPlane) -> AutomaticSslService:
    existing = _automatic_ssl_services.get(platform)
    if existing is not None:
        return existing
    service = AutomaticSslService(
        sites=platform.sites,
        # The one declared optional-to-optional edge in the workspace. The
        # scan's question — "can every edge reach this origin, and would it
        # still under Full (strict)?" — is `blitzecdn-origins`' play, and core
        # carries no method for it. Declared in this distribution's
        # dependencies rather than imported opportunistically, so pip installs
        # both and detaching that package cannot leave this import dangling.
        runner=platform.origin_checks,
        origin_probe=platform.origin_probe,
        site_editor=platform.site_editor,
        deployments=platform.deployments,
    )
    _automatic_ssl_services[platform] = service
    return service
