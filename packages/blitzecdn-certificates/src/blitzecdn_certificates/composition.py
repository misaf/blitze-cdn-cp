"""Wire certificate operations from public control-plane ports."""

from typing import TYPE_CHECKING, cast
from weakref import WeakKeyDictionary

from blitzecdn_origins.adapters import OriginCheckPlaybook

from blitzecdn_certificates.automatic_ssl.service import AutomaticSslService
from blitzecdn_certificates.certificates.adapters import CertbotIssuer, CertificateStore
from blitzecdn_certificates.certificates.ports import WorkflowCoordinator
from blitzecdn_certificates.certificates.preflight import CertificatePreflight
from blitzecdn_certificates.certificates.service import (
    CertificateExecution,
    CertificatePersistence,
    CertificatePolicy,
    CertificateService,
)

if TYPE_CHECKING:
    from blitzecdn.bootstrap import ControlPlane

__version__ = "3.0.0"

_certificate_services: WeakKeyDictionary[object, CertificateService] = (
    WeakKeyDictionary()
)
_automatic_ssl_services: WeakKeyDictionary[object, AutomaticSslService] = (
    WeakKeyDictionary()
)


def build_certificate_service(platform: ControlPlane) -> CertificateService:
    existing = _certificate_services.get(platform)
    if existing is not None:
        return existing
    service = CertificateService(
        policy=CertificatePolicy(default_email=platform.settings.acme_default_email),
        persistence=CertificatePersistence(
            sites=platform.sites,
            certificates=CertificateStore(platform.settings),
            uow=platform.transactions,
            requirements=platform.deployment_requirements,
        ),
        execution=CertificateExecution(
            runner=platform.deployment_lock,
            issuer=CertbotIssuer(platform.settings),
            preflight=CertificatePreflight(
                platform.settings,
                platform.edge_inventory,
                origin_probe=platform.origin_probe,
            ),
        ),
        events=platform.events,
        dns=platform.dns,
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
        dns=platform.dns,
        deployments=platform.deployments,
    )
    _automatic_ssl_services[platform] = service
    return service
