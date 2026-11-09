"""The concrete adapters one control-plane process runs on.

Services are built from these and never construct an adapter. :class:`Adapters`
is the one test seam: replace an adapter, keep the production graph. Each
capability's own stores and services come from its `wiring.py` provider.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from dishka import Provider, Scope, alias, from_context, provide

from blitzecdn.capabilities.deployments.adapters.serving import ServingProbe
from blitzecdn.capabilities.deployments.ports import (
    DeploymentRunner,
    QueueBackgroundRunner,
    ServingVerifier,
)
from blitzecdn.capabilities.deployments.settings import DeploymentsConfig
from blitzecdn.capabilities.deployments.settings import (
    lock_path as deployment_lock_path,
)
from blitzecdn.capabilities.edges.adapters.host_keys import KnownHosts
from blitzecdn.capabilities.edges.adapters.persistence import EdgeStore
from blitzecdn.capabilities.edges.adapters.probe import OriginProbe
from blitzecdn.capabilities.edges.adapters.roster import EdgeRoster
from blitzecdn.capabilities.edges.ports import EdgeStore as EdgeStorePort
from blitzecdn.capabilities.edges.ports import HostKeyStore as HostKeyStorePort
from blitzecdn.capabilities.edges.ports import OriginProbe as OriginProbePort
from blitzecdn.capabilities.jobs.service import JobQueue
from blitzecdn.capabilities.origins.settings import OriginsConfig
from blitzecdn.composition.repository import Repository
from blitzecdn.composition.roster import load_registry
from blitzecdn.core.ansible import AnsibleRunner
from blitzecdn.core.ansible.contributions import EdgeContributions
from blitzecdn.core.ansible.probe import release_probe_token
from blitzecdn.core.config import Settings
from blitzecdn.core.features import (
    FeatureRegistry,
    resolve_capability_environment,
    resolve_edge_capability_roles,
    resolve_edge_modules,
    resolve_host_capability_roles,
    resolve_nginx_resources,
    resolve_role_search_path,
    resolve_teardown_capability_roles,
)
from blitzecdn.core.features.resolution import ResolvedCapabilityEnvironment
from blitzecdn.core.persistence.configuration import AnsibleSettingStore
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.clock import Clock, system_clock
from blitzecdn.core.ports.operations import (
    AuditTrail,
    EventRecorder,
    FleetSettings,
    PlaybookRunner,
)

__all__ = ["Adapters", "FleetRunner", "InfrastructureProvider"]


class FleetRunner(DeploymentRunner, PlaybookRunner, Protocol):
    """Every runner port the one Ansible adapter satisfies."""


@dataclass(frozen=True, slots=True)
class Adapters:
    """Adapters a caller supplies in place of the real ones; ``None`` builds it.

    A supplied adapter stays its caller's: the container never closes it, so
    two containers may share a store.
    """

    repository: Repository | None = None
    features: FeatureRegistry | None = None
    runner: FleetRunner | None = None
    origin_probe: OriginProbePort | None = None
    host_keys: HostKeyStorePort | None = None
    serving_probe: ServingVerifier | None = None
    edges: EdgeStorePort | None = None
    background: QueueBackgroundRunner | None = None
    #: The one clock every service and store in this process reads.
    clock: Clock = system_clock
    pool_connections: bool = False


class InfrastructureProvider(Provider):
    """The adapters, and the feature contributions resolved once for them."""

    scope = Scope.APP

    settings = from_context(provides=Settings)
    adapters = from_context(provides=Adapters)

    @provide
    def repository(
        self, settings: Settings, adapters: Adapters
    ) -> Iterator[Repository]:
        if adapters.repository is not None:
            yield adapters.repository
            return
        store = Repository(
            settings.database_path,
            pool_connections=adapters.pool_connections,
            audit_retention=settings.audit_retention,
            clock=adapters.clock,
        )
        try:
            yield store
        finally:
            store.close()

    @provide
    def features(self, adapters: Adapters) -> FeatureRegistry:
        return adapters.features if adapters.features is not None else load_registry()

    @provide
    def capability_config(
        self, settings: Settings, features: FeatureRegistry
    ) -> ResolvedCapabilityEnvironment:
        return resolve_capability_environment(
            features.configuration_contributions(),
            settings.capability_environment,
            settings.capability_config_file,
            settings.state_dir,
        )

    @provide
    def edge_contributions(
        self,
        settings: Settings,
        features: FeatureRegistry,
        capability_config: ResolvedCapabilityEnvironment,
    ) -> EdgeContributions:
        """What the features add to a run, resolved once."""
        contributions = features.ansible_contributions()
        return EdgeContributions.of(
            roles_path=resolve_role_search_path(
                settings.ansible_dir / "roles", contributions
            ),
            edge_roles=resolve_edge_capability_roles(contributions),
            host_roles=resolve_host_capability_roles(contributions),
            teardown_roles=resolve_teardown_capability_roles(contributions),
            nginx_resources=resolve_nginx_resources(features.nginx_contributions()),
            edge_modules=resolve_edge_modules(contributions),
            environment=capability_config.environment,
        )

    @provide
    def clock(self, adapters: Adapters) -> Clock:
        """The one clock every service and store in this process reads."""
        return adapters.clock

    @provide
    def database(self, repository: Repository) -> Database:
        return repository.database

    @provide
    def unit_of_work(self, repository: Repository) -> UnitOfWork:
        return repository

    @provide
    def events(self, repository: Repository) -> EventRecorder:
        return repository.audit_log

    @provide
    def ansible_settings(self, repository: Repository) -> AnsibleSettingStore:
        return repository.ansible_settings

    @provide
    def edges(self, database: Database, adapters: Adapters) -> EdgeStorePort:
        # Here rather than in the edges provider: a test may replace it.
        return adapters.edges or EdgeStore(database)

    @provide
    def runner(
        self,
        settings: Settings,
        adapters: Adapters,
        edges: EdgeStorePort,
        edge_contributions: EdgeContributions,
        deployments_config: DeploymentsConfig,
    ) -> FleetRunner:
        # The fleet as core reads it, so `core.ansible` imports no capability.
        return adapters.runner or AnsibleRunner(
            settings,
            EdgeRoster(edges),
            edge_contributions,
            timeout_seconds=deployments_config.timeout_seconds,
            lock_path=deployment_lock_path(settings.state_dir),
        )

    playbooks = alias(source=FleetRunner, provides=PlaybookRunner)
    deployment_runner = alias(source=FleetRunner, provides=DeploymentRunner)

    @provide
    def origin_probe(
        self, adapters: Adapters, origins_config: OriginsConfig
    ) -> OriginProbePort:
        return adapters.origin_probe or OriginProbe(
            timeout_seconds=origins_config.check_timeout_seconds
        )

    @provide
    def host_keys(self, settings: Settings, adapters: Adapters) -> HostKeyStorePort:
        return adapters.host_keys or KnownHosts(settings)

    @provide
    def serving_probe(self, settings: Settings, adapters: Adapters) -> ServingVerifier:
        return adapters.serving_probe or ServingProbe(
            token=release_probe_token(settings.state_dir)
        )

    @provide
    def background(self, adapters: Adapters, queue: JobQueue) -> QueueBackgroundRunner:
        return adapters.background or _QueuePublisher(queue)

    # -- stores entry layers read directly -------------------------------

    @provide
    def audit(self, repository: Repository) -> AuditTrail:
        """The read side of the audit trail, so no entry layer can record."""
        return repository.audit_log

    fleet_settings = alias(source=AnsibleSettingStore, provides=FleetSettings)


class _QueuePublisher:
    """The deployments ``QueueBackgroundRunner`` port, bound to the job queue."""

    def __init__(self, queue: JobQueue) -> None:
        self._queue = queue

    def enqueue(self, deployment_id: str) -> None:
        self._queue.enqueue_deployment(deployment_id)
