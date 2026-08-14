"""The composition root.

This is the one module that knows both halves: it builds the concrete adapters
from :mod:`blitzecdn.infrastructure` and injects them into the pure services in
:mod:`blitzecdn.application`. Production wiring lives here and nowhere else, so
"what does a real control plane consist of" is answerable by reading one
constructor.

``ControlPlane`` is that constructor and nothing else. It holds the four
services and the ports the entry layers read through, and it forwards no calls:
the CLI and the API reach the service that owns the work —
``control_plane.dns.create_record(...)`` — rather than a method here that would
restate a signature already written on the service.

Everything reachable from here is a service or a port. The concrete
``Repository`` is deliberately not an attribute: an entry layer that could
reach it would be one import away from calling SQLite directly, which is easy
to do by accident in a read path and invisible in review — so the rule is
written down rather than assumed.
"""

from __future__ import annotations

from collections.abc import Callable

from blitzecdn.application import (
    CacheService,
    CertificateExecution,
    CertificatePersistence,
    CertificateService,
    DeploymentExecution,
    DeploymentPersistence,
    DeploymentService,
    DnsService,
    EdgeOperationsService,
)
from blitzecdn.application.deployment_support import DesiredStateRenderer
from blitzecdn.application.workflows import WorkflowCoordinator
from blitzecdn.config import Settings
from blitzecdn.infrastructure.ansible import AnsibleRunner
from blitzecdn.infrastructure.certificates import CertbotIssuer, CertificateStore
from blitzecdn.infrastructure.database import Repository
from blitzecdn.infrastructure.filesystem import atomic_write_yaml, read_log_tail
from blitzecdn.infrastructure.origins import OriginProbe
from blitzecdn.infrastructure.preflight import CertificatePreflight
from blitzecdn.infrastructure.process import DramatiqBackgroundRunner
from blitzecdn.infrastructure.queue import redis_ready
from blitzecdn.ports import (
    AuditTrail,
    DeploymentRunner,
    Issuer,
    Preflight,
    QueueBackgroundRunner,
)
from blitzecdn.ports import (
    CertificateStore as CertificateStorePort,
)
from blitzecdn.ports import (
    EdgeStore as EdgeStorePort,
)
from blitzecdn.ports import (
    OriginProbe as OriginProbePort,
)


class ControlPlane:
    """The wired services, and the ports the entry layers read through."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Repository | None = None,
        runner: DeploymentRunner | None = None,
        certificate_store: CertificateStorePort | None = None,
        issuer: Issuer | None = None,
        origin_probe: OriginProbePort | None = None,
        preflight: Preflight | None = None,
        edges_store: EdgeStorePort | None = None,
        background: QueueBackgroundRunner | None = None,
        broker_ready: Callable[[str], bool] | None = None,
        pool_connections: bool = False,
    ) -> None:
        self.settings = settings
        store = repository or Repository(
            settings.database_path, pool_connections=pool_connections
        )
        self._owned_repository = store if repository is None else None
        self._wire_adapters(
            store=store,
            runner=runner,
            certificate_store=certificate_store,
            issuer=issuer,
            origin_probe=origin_probe,
            preflight=preflight,
            edges_store=edges_store,
            background=background,
            broker_ready=broker_ready,
        )
        self._wire_services(store)

    def _wire_adapters(
        self,
        *,
        store: Repository,
        runner: DeploymentRunner | None,
        certificate_store: CertificateStorePort | None,
        issuer: Issuer | None,
        origin_probe: OriginProbePort | None,
        preflight: Preflight | None,
        edges_store: EdgeStorePort | None,
        background: QueueBackgroundRunner | None,
        broker_ready: Callable[[str], bool] | None,
    ) -> None:
        """Choose concrete outside-world capabilities and their test overrides."""
        # The fleet, and the rows the `blitzecdn` Ansible inventory plugin reads
        # for itself at the start of every run. Both the runner and preflight
        # take it so that "which edges exist" has exactly one answer, whoever is
        # asking and whichever process they are in.
        self.edges_store = edges_store or store.edges
        self.ansible_settings = store.ansible_settings
        self.runner = runner or AnsibleRunner(self.settings, self.edges_store)
        self.certificate_store = certificate_store or CertificateStore(self.settings)
        self.issuer = issuer or CertbotIssuer(self.settings)
        self.origin_probe = origin_probe or OriginProbe(self.settings)
        self.preflight = preflight or CertificatePreflight(
            self.settings, self.edges_store, origin_probe=self.origin_probe
        )
        self.background = background or DramatiqBackgroundRunner(
            str(self.settings.redis_url)
        )
        readiness_probe = broker_ready or redis_ready
        self._broker_ready: Callable[[], bool] = lambda: readiness_probe(
            str(self.settings.redis_url)
        )

    def _wire_services(self, store: Repository) -> None:
        """Build cross-cutting services, then feature-oriented services."""

        # Entry layers receive only the read side of the audit trail, so they
        # cannot manufacture an event for an action no service performed.
        self.audit: AuditTrail = store.audit_log

        # The same audit adapter is exposed read-only to entry layers and as an
        # event recorder to services. There is one durable consumer, so a
        # generic observer registry would only disguise this ownership.
        self.events = store.audit_log
        self.workflows = WorkflowCoordinator(
            journal=store.workflows,
            uow=store,
            retention=self.settings.history_retention,
        )
        self.workflow_history = store.workflows
        self.deployment_requirements = store.deployment_requirements

        # Each store is passed where its port is asked for, so a service is
        # handed the slice of persistence it declared and no more.
        self.dns = DnsService(
            zones=store.zones,
            sites=store.sites,
            events=self.events,
            uow=store,
        )
        self._wire_feature_services(store)

    def _wire_feature_services(self, store: Repository) -> None:
        """Build deployment, certificate, edge, and cache capabilities."""
        renderer = DesiredStateRenderer(
            settings=self.settings,
            certificates=self.certificate_store,
            write_yaml=atomic_write_yaml,
        )
        self.deployments = DeploymentService(
            settings=self.settings,
            persistence=DeploymentPersistence(
                deployments=store.deployments,
                zones=store.zones,
                uow=store,
                requirements=store.deployment_requirements,
            ),
            execution=DeploymentExecution(
                runner=self.runner,
                background=self.background,
                read_log=read_log_tail,
                renderer=renderer,
            ),
            events=self.events,
            dns=self.dns,
            workflows=self.workflows,
        )
        self.certificates = CertificateService(
            settings=self.settings,
            persistence=CertificatePersistence(
                sites=store.sites,
                certificates=self.certificate_store,
                uow=store,
                requirements=store.deployment_requirements,
            ),
            execution=CertificateExecution(
                runner=self.runner,
                issuer=self.issuer,
                preflight=self.preflight,
            ),
            events=self.events,
            dns=self.dns,
            deployments=self.deployments,
            workflows=self.workflows,
        )
        self.edges = EdgeOperationsService(
            sites=store.sites,
            events=self.events,
            runner=self.runner,
            origin_probe=self.origin_probe,
            edges=self.edges_store,
            uow=store,
        )
        # Reads sites to decide what may be purged and never writes one, so it
        # is handed the store rather than the zone editor: purging is not a
        # change to desired state and must not be able to become one.
        self.cache = CacheService(
            sites=store.sites,
            events=self.events,
            runner=self.runner,
        )

    def broker_ready(self) -> bool:
        """Whether the durable work broker is currently reachable."""
        return self._broker_ready()

    def close(self) -> None:
        """Release adapters created by this composition root.

        Injected repositories remain the caller's responsibility. This keeps
        tests and embedded callers free to share one repository between more
        than one control plane without one instance closing another's store.
        """
        repository, self._owned_repository = self._owned_repository, None
        if repository is not None:
            repository.close()


def build_control_plane(
    settings: Settings,
    *,
    pool_connections: bool = False,
) -> ControlPlane:
    """Build a control plane wired to the real adapters."""
    return ControlPlane(settings=settings, pool_connections=pool_connections)
