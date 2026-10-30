"""The application's service graph, defined once.

:class:`ServiceProvider` decides which collaborator goes where; each
capability's `wiring.py` does the building. Processes are in
:mod:`blitzecdn.composition.processes`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, NewType, cast

from dishka import Provider, Scope, alias, provide

from blitzecdn.assets import ansible as core_ansible
from blitzecdn.capabilities.backup.service import BackupService
from blitzecdn.capabilities.backup.wiring import build_backup_service
from blitzecdn.capabilities.cache.service import CacheService
from blitzecdn.capabilities.cache.service import operations as cache_operations
from blitzecdn.capabilities.cache.wiring import build_cache_service
from blitzecdn.capabilities.deployments.ports import (
    QueueBackgroundRunner,
    ServingVerifier,
)
from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.capabilities.deployments.wiring import build_deployment_service
from blitzecdn.capabilities.diagnostics.service.status import DiagnosticsService
from blitzecdn.capabilities.dns import DnsService
from blitzecdn.capabilities.dns.ports import SiteReader
from blitzecdn.capabilities.dns.service import HostService, RuleService
from blitzecdn.capabilities.dns.wiring import (
    build_dns_service,
    build_host_service,
    build_rule_service,
)
from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.edges.ports import EdgeStore as EdgeStorePort
from blitzecdn.capabilities.edges.ports import OriginProbe as OriginProbePort
from blitzecdn.capabilities.edges.service import operations as edge_operations
from blitzecdn.capabilities.edges.wiring import build_edge_operations_service
from blitzecdn.capabilities.jobs.ports import WorkerHistory, WorkerProgress
from blitzecdn.capabilities.jobs.service import (
    JobQueue,
    JobRunner,
    JobScheduler,
    OperationService,
)
from blitzecdn.capabilities.maintenance import MaintenanceService
from blitzecdn.capabilities.maintenance.wiring import build_maintenance_service
from blitzecdn.capabilities.origins.adapters import OriginCheckPlaybook
from blitzecdn.capabilities.origins.service import operations as origin_operations
from blitzecdn.capabilities.origins.service.checks import OriginCheckService
from blitzecdn.capabilities.origins.wiring import build_origin_check_service
from blitzecdn.capabilities.releases.service import ReleaseService
from blitzecdn.capabilities.releases.wiring import build_release_service
from blitzecdn.capabilities.tls.automatic_ssl.service import AutomaticSslService
from blitzecdn.capabilities.tls.certificates.adapters import CertificateStore
from blitzecdn.capabilities.tls.certificates.ports import SiteEditor
from blitzecdn.capabilities.tls.certificates.ports import (
    WorkflowCoordinator as CertificateWorkflows,
)
from blitzecdn.capabilities.tls.certificates.service import CertificateService
from blitzecdn.capabilities.tls.settings import CertificateConfig
from blitzecdn.capabilities.tls.wiring import (
    build_automatic_ssl_service,
    build_certificate_service,
    build_site_editor,
)
from blitzecdn.capabilities.traffic.service.collection import TrafficService
from blitzecdn.capabilities.traffic.settings import TrafficConfig
from blitzecdn.capabilities.traffic.wiring import build_traffic_service
from blitzecdn.capabilities.workflows.service import WorkflowCoordinator
from blitzecdn.composition.infrastructure import (
    Adapters,
    FleetRunner,
    QueueReadiness,
)
from blitzecdn.composition.repository import Repository
from blitzecdn.composition.roster import (
    OperationInputs,
    RenderingInputs,
    bind_operations,
    bind_renderings,
)
from blitzecdn.core.ansible.contributions import EdgeContributions
from blitzecdn.core.ansible.rendering import EdgeRenderer
from blitzecdn.core.config import Settings
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.features import FeatureRegistry, Operations, Rendering
from blitzecdn.core.features.resolution import ResolvedCapabilityEnvironment

__all__ = ["OperationRun", "OperationRuns", "ServiceProvider"]

#: One edge operation's run: its JSON payload in, its JSON report out.
OperationRun = Callable[[Mapping[str, Any]], dict[str, Any]]

#: Every edge operation the API may queue, by kind, and its run in the worker.
OperationRuns = NewType("OperationRuns", dict[str, OperationRun])


class ServiceProvider(Provider):
    """Every service, each handed exactly the collaborators it declared."""

    scope = Scope.APP

    sites = alias(source=HostService, provides=SiteReader)

    @provide
    def diagnostics(
        self,
        repository: Repository,
        certificates: CertificateService,
        workers: WorkerHistory,
        backups: BackupService,
        adapters: Adapters,
    ) -> DiagnosticsService:
        return DiagnosticsService(
            queue=repository.jobs,
            workflows=repository.workflows,
            certificates=certificates,
            workers=workers,
            backups=backups,
            clock=adapters.clock,
        )

    @provide
    def workflows(
        self, settings: Settings, adapters: Adapters, repository: Repository
    ) -> WorkflowCoordinator:
        return WorkflowCoordinator(
            journal=repository.workflows,
            uow=repository,
            retention=settings.history_retention,
            clock=adapters.clock,
        )

    # -- DNS owns canonical zones, rules, records, and their host projection.

    @provide
    def dns(self, repository: Repository) -> DnsService:
        return build_dns_service(
            zones=repository.zones,
            rules=repository.rules,
            events=repository.audit_log,
            uow=repository,
        )

    @provide
    def hosts(self, repository: Repository) -> HostService:
        return build_host_service(
            zones=repository.zones,
            rules=repository.rules,
            events=repository.audit_log,
            uow=repository,
        )

    @provide
    def site_editor(self, hosts: HostService) -> SiteEditor:
        # DNS writes where a host's policy comes from; TLS decides what it writes.
        return build_site_editor(writer=hosts)

    @provide
    def rules(self, repository: Repository) -> RuleService:
        # A rule may consult the zone policy it overrides and may never write one.
        return build_rule_service(
            rules=repository.rules,
            zones=repository.zones,
            events=repository.audit_log,
            uow=repository,
        )

    # -- releases and their rollout ----------------------------------------

    @provide
    def rendering(
        self,
        features: FeatureRegistry,
        capability_config: ResolvedCapabilityEnvironment,
        certificate_store: CertificateStore,
    ) -> Rendering[CdnSite]:
        """What every feature renders into a release, from configuration and
        adapters only."""
        return features.bind_rendering(
            bind_renderings(
                RenderingInputs(
                    capability_config=capability_config,
                    certificate_store=certificate_store,
                )
            )
        )

    @provide
    def releases(
        self,
        settings: Settings,
        repository: Repository,
        features: FeatureRegistry,
        edges: EdgeStorePort,
        rendering: Rendering[CdnSite],
        edge_contributions: EdgeContributions,
    ) -> ReleaseService:
        return build_release_service(
            state=repository,
            releases=repository.releases,
            edges=edges,
            contributors=rendering,
            objections=rendering.site_objections,
            capabilities=tuple(features.capabilities),
            allow_empty_sites=settings.allow_empty_sites,
            retention=settings.history_retention,
            uow=repository,
            # The shipped templates and defaults, from the package itself,
            # whatever directory its playbooks run from.
            renderer=EdgeRenderer(
                ansible_dir=core_ansible.ROLES_PATH.parent,
                contributions=edge_contributions,
            ),
        )

    @provide
    def deployments(
        self,
        settings: Settings,
        adapters: Adapters,
        repository: Repository,
        releases: ReleaseService,
        edges: EdgeStorePort,
        verifier: ServingVerifier,
        runner: FleetRunner,
        background: QueueBackgroundRunner,
        dns: DnsService,
        workflows: WorkflowCoordinator,
    ) -> DeploymentService:
        return build_deployment_service(
            settings=settings,
            deployments=repository.deployments,
            zones=repository.zones,
            rules=repository.rules,
            fleet_settings=repository.ansible_settings,
            requirements=repository.deployment_requirements,
            releases=releases,
            targets=repository.deployment_targets,
            edges=edges,
            verifier=verifier,
            runner=runner,
            background=background,
            dns=dns,
            workflows=workflows,
            events=repository.audit_log,
            uow=repository,
            clock=adapters.clock,
        )

    # -- edge operations ---------------------------------------------------

    @provide
    def edge_operations(
        self, repository: Repository, edges: EdgeStorePort, runner: FleetRunner
    ) -> EdgeOperationsService:
        return build_edge_operations_service(
            edges=edges, runner=runner, events=repository.audit_log, uow=repository
        )

    @provide
    def traffic_config(
        self, capability_config: ResolvedCapabilityEnvironment
    ) -> TrafficConfig:
        return capability_config.for_feature("traffic").as_model(TrafficConfig)

    @provide
    def traffic(
        self,
        repository: Repository,
        runner: FleetRunner,
        traffic_config: TrafficConfig,
        adapters: Adapters,
    ) -> TrafficService:
        return build_traffic_service(
            store=repository.traffic,
            fleet=runner,
            events=repository.audit_log,
            uow=repository,
            config=traffic_config,
            edge_names=lambda: [edge.name for edge in repository.edges.list_edges()],
            clock=adapters.clock,
        )

    @provide
    def cache(
        self,
        adapters: Adapters,
        repository: Repository,
        sites: HostService,
        runner: FleetRunner,
    ) -> CacheService:
        return build_cache_service(
            sites=sites,
            events=repository.audit_log,
            fleet=runner,
            clock=adapters.clock,
        )

    @provide
    def origins(
        self,
        adapters: Adapters,
        repository: Repository,
        sites: HostService,
        runner: FleetRunner,
        origin_probe: OriginProbePort,
    ) -> OriginCheckService:
        return build_origin_check_service(
            sites=sites,
            events=repository.audit_log,
            fleet=runner,
            origin_probe=origin_probe,
            clock=adapters.clock,
        )

    @provide
    def operation_runs(
        self,
        cache: CacheService,
        origins: OriginCheckService,
        edges: EdgeOperationsService,
    ) -> OperationRuns:
        return OperationRuns(
            {
                **cache_operations.operation_handlers(cache),
                **origin_operations.operation_handlers(origins),
                **edge_operations.operation_handlers(edges),
            }
        )

    @provide
    def queued_operations(
        self, queue: JobQueue, runs: OperationRuns
    ) -> OperationService:
        return OperationService(queue=queue, kinds=runs)

    # -- TLS ---------------------------------------------------------------

    # -- backup opens no repository; the host timer runs it, diagnostics reads it.

    @provide
    def backups(self, settings: Settings, features: FeatureRegistry) -> BackupService:
        return build_backup_service(
            settings, configuration=features.configuration_contributions()
        )

    @provide
    def certificate_config(
        self, capability_config: ResolvedCapabilityEnvironment
    ) -> CertificateConfig:
        return capability_config.for_feature("certificates").as_model(CertificateConfig)

    @provide
    def certificates(
        self,
        settings: Settings,
        adapters: Adapters,
        repository: Repository,
        config: CertificateConfig,
        store: CertificateStore,
        releases: ReleaseService,
        hosts: HostService,
        site_editor: SiteEditor,
        edges: EdgeStorePort,
        origin_probe: OriginProbePort,
        runner: FleetRunner,
        dns: DnsService,
        deployments: DeploymentService,
        workflows: WorkflowCoordinator,
    ) -> CertificateService:
        return build_certificate_service(
            settings=settings,
            config=config,
            store=store,
            releases=releases,
            sites=hosts,
            edges=edges,
            origin_probe=origin_probe,
            requirements=repository.deployment_requirements,
            lock=runner,
            events=repository.audit_log,
            dns=dns,
            site_editor=site_editor,
            deployments=deployments,
            # ``@contextmanager`` exposes its concrete private context-manager
            # type to mypy. The behavior is the narrow protocol TLS declares.
            workflows=cast(CertificateWorkflows, workflows),
            uow=repository,
            clock=adapters.clock,
        )

    @provide
    def automatic_ssl(
        self,
        hosts: HostService,
        site_editor: SiteEditor,
        runner: FleetRunner,
        origin_probe: OriginProbePort,
        deployments: DeploymentService,
    ) -> AutomaticSslService:
        return build_automatic_ssl_service(
            sites=hosts,
            origin_checks=OriginCheckPlaybook(runner),
            origin_probe=origin_probe,
            site_editor=site_editor,
            deployments=deployments,
        )

    # -- contributed jobs and the queue's runner ----------------------------

    @provide
    def operations(
        self,
        settings: Settings,
        repository: Repository,
        features: FeatureRegistry,
        deployments: DeploymentService,
        certificates: CertificateService,
        automatic_ssl: AutomaticSslService,
        certificate_config: CertificateConfig,
        queue_ready: QueueReadiness,
        traffic: TrafficService,
        traffic_config: TrafficConfig,
    ) -> Operations:
        """Contributed jobs close over services, so they bind once every
        service exists."""
        return features.bind_operations(
            bind_operations(
                OperationInputs(
                    settings=settings,
                    deployments=deployments,
                    certificates=certificates,
                    automatic_ssl=automatic_ssl,
                    certificate_config=certificate_config,
                    read_database=lambda: repository.workflows.list_workflows(1),
                    queue_ready=queue_ready,
                    traffic=traffic,
                    traffic_config=traffic_config,
                    has_edges=lambda: bool(repository.edges.list_edges()),
                )
            )
        )

    @provide
    def maintenance(
        self, operations: Operations, deployments: DeploymentService
    ) -> MaintenanceService:
        return build_maintenance_service(
            jobs=operations.scheduled_jobs, deployments=deployments
        )

    @provide
    def job_runner(
        self,
        settings: Settings,
        adapters: Adapters,
        queue: JobQueue,
        scheduler: JobScheduler,
        progress: WorkerProgress,
    ) -> JobRunner:
        return JobRunner(
            queue=queue,
            scheduler=scheduler,
            poll_seconds=settings.worker_poll_seconds,
            clock=adapters.clock,
            progress=progress,
        )
