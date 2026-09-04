"""Wire certificate operations from public control-plane ports."""

from typing import TYPE_CHECKING, cast
from weakref import WeakKeyDictionary

from blitzecdn_origins.adapters import OriginCheckPlaybook

from blitzecdn.core.runtime.resources import distribution_version
from blitzecdn_certificates.automatic_ssl.service import AutomaticSslService
from blitzecdn_certificates.certificates.adapters import CertbotIssuer, CertificateStore
from blitzecdn_certificates.certificates.adapters.preflight import CertificatePreflight
from blitzecdn_certificates.certificates.ports import WorkflowCoordinator
from blitzecdn_certificates.certificates.service import (
    CertificateExecution,
    CertificatePersistence,
    CertificatePolicy,
    CertificateService,
)
from blitzecdn_certificates.config import CertificateConfig

if TYPE_CHECKING:
    from blitzecdn.bootstrap import ControlPlane

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


def build_certificate_service(platform: ControlPlane) -> CertificateService:
    existing = _certificate_services.get(platform)
    if existing is not None:
        return existing
    config = certificate_config(platform)
    service = CertificateService(
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
        # carries no method for it any more. Declared in this distribution's
        # dependencies rather than imported opportunistically, so pip installs
        # both and detaching that package cannot leave this import dangling.
        runner=OriginCheckPlaybook(platform.fleet),
        origin_probe=platform.origin_probe,
        site_editor=platform.site_editor,
        deployments=platform.deployments,
    )
    _automatic_ssl_services[platform] = service
    return service
