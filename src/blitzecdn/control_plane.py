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

from blitzecdn.application import (
    CacheService,
    CertificateService,
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
        settings: Settings,
        repository: Repository | None = None,
        runner: DeploymentRunner | None = None,
        certificate_store: CertificateStorePort | None = None,
        issuer: Issuer | None = None,
        origin_probe: OriginProbePort | None = None,
        preflight: Preflight | None = None,
        edges_store: EdgeStorePort | None = None,
        background: BackgroundRunner | QueueBackgroundRunner | None = None,
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

        # The audit trail as a read-only port. It is written by the observer
        # below, never by a caller, so nothing here can record an event no
        # service actually announced.
        self.audit: AuditTrail = store.audit_log

        # The one subscriber the audit trail needs is registered here, so the
        # services can publish "something happened" without knowing who listens.
        self.bus = InProcessEventBus()
        self.bus.subscribe(AuditObserver(store.audit_log))
        self.workflows = WorkflowCoordinator(
            store.workflows, store, settings.history_retention
        )
        self.workflow_history = store.workflows
        self.recovery = RecoveryService(store.workflows, store)

        # Each store is passed where its port is asked for, so a service is
        # handed the slice of persistence it declared and no more.
        self.dns = DnsService(store.zones, store.sites, self.bus, store)
        renderer = DesiredStateRenderer(
            settings, self.certificate_store, atomic_write_yaml
        )
        self.deployments = DeploymentService(
            settings,
            store.deployments,
            store.zones,
            self.bus,
            self.runner,
            self.dns,
            self.background,
            read_log_tail,
            store,
            self.workflows,
            renderer,
            RollbackPlanner(store.deployments),
            DriftInterpreter(),
        )
        self.certificates = CertificateService(
            settings,
            store.sites,
            self.bus,
            self.runner,
            self.certificate_store,
            self.issuer,
            self.preflight,
            self.dns,
            self.deployments,
            store,
            self.workflows,
        )
        self.edges = EdgeOperationsService(
            store.sites,
            self.bus,
            self.runner,
            self.origin_probe,
            self.edges_store,
            store,
        )
        # Reads sites to decide what may be purged and never writes one, so it
        # is handed the store rather than the zone editor: purging is not a
        # change to desired state and must not be able to become one.
        self.cache = CacheService(store.sites, self.bus, self.runner)

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
    return ControlPlane(settings, pool_connections=pool_connections)
