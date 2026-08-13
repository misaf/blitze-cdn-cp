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
from blitzecdn.application.deployment_support import (
    DesiredStateRenderer,
    DriftInterpreter,
    RollbackPlanner,
)
from blitzecdn.application.workflows import RecoveryService, WorkflowCoordinator
from blitzecdn.config import Settings
from blitzecdn.infrastructure.ansible import AnsibleRunner
from blitzecdn.infrastructure.certificates import CertbotIssuer, CertificateStore
from blitzecdn.infrastructure.database import Repository
from blitzecdn.infrastructure.events import AuditObserver, InProcessEventBus
from blitzecdn.infrastructure.filesystem import atomic_write_yaml, read_log_tail
from blitzecdn.infrastructure.origins import OriginProbe
from blitzecdn.infrastructure.preflight import CertificatePreflight
from blitzecdn.infrastructure.process import DramatiqBackgroundRunner
from blitzecdn.infrastructure.queue import redis_ready
from blitzecdn.ports import (
    AuditTrail,
    BackgroundRunner,
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
        background: BackgroundRunner | QueueBackgroundRunner | None = None,
        broker_ready: Callable[[str], bool] | None = None,
        pool_connections: bool = False,
    ) -> None:
        self.settings = settings
        store = repository or Repository(
            settings.database_path, pool_connections=pool_connections
        )
        self._owned_repository = store if repository is None else None
        # The fleet, and the rows the `blitzecdn` Ansible inventory plugin reads
        # for itself at the start of every run. Both the runner and preflight
        # take it so that "which edges exist" has exactly one answer, whoever is
        # asking and whichever process they are in.
        self.edges_store = edges_store or store.edges
        self.ansible_settings = store.ansible_settings
        self.runner = runner or AnsibleRunner(settings, self.edges_store)
        self.certificate_store = certificate_store or CertificateStore(settings)
        self.issuer = issuer or CertbotIssuer(settings)
        self.origin_probe = origin_probe or OriginProbe(settings)
        self.preflight = preflight or CertificatePreflight(
            settings, self.edges_store, origin_probe=self.origin_probe
        )
        self.background = background or DramatiqBackgroundRunner(
            str(settings.redis_url)
        )
        readiness_probe = broker_ready or redis_ready
        self._broker_ready: Callable[[], bool] = lambda: readiness_probe(
            str(settings.redis_url)
        )

        # The audit trail as a read-only port. It is written by the observer
        # below, never by a caller, so nothing here can record an event no
        # service actually announced.
        self.audit: AuditTrail = store.audit_log

        # The one subscriber the audit trail needs is registered here, so the
        # services can publish "something happened" without knowing who listens.
        self.bus = InProcessEventBus()
        self.bus.subscribe(AuditObserver(store.audit_log))
        self.workflows = WorkflowCoordinator(
            journal=store.workflows,
            uow=store,
            retention=settings.history_retention,
        )
        self.workflow_history = store.workflows
        self.deployment_requirements = store.deployment_requirements
        self.recovery = RecoveryService(journal=store.workflows, uow=store)

        # Each store is passed where its port is asked for, so a service is
        # handed the slice of persistence it declared and no more.
        self.dns = DnsService(
            zones=store.zones,
            sites=store.sites,
            bus=self.bus,
            uow=store,
        )
        renderer = DesiredStateRenderer(
            settings=settings,
            certificates=self.certificate_store,
            write_yaml=atomic_write_yaml,
        )
        self.deployments = DeploymentService(
            settings=settings,
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
            bus=self.bus,
            dns=self.dns,
            workflows=self.workflows,
            rollbacks=RollbackPlanner(deployments=store.deployments),
            drift=DriftInterpreter(),
        )
        self.certificates = CertificateService(
            settings=settings,
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
            bus=self.bus,
            dns=self.dns,
            deployments=self.deployments,
            workflows=self.workflows,
        )
        self.edges = EdgeOperationsService(
            sites=store.sites,
            bus=self.bus,
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
            bus=self.bus,
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
