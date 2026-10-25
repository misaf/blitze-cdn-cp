"""The application's service graph, defined once.

:func:`build_services` turns an :class:`~blitzecdn.composition.Infrastructure`
into every service the application has. Each capability's own
`wiring.py` builds its service from collaborators passed by name; this
module only decides which collaborator goes where, and in which order the
graph is built.

There is no lifecycle and no process branch here. Which of these a process
uses, and what it does when it starts and stops, is
:mod:`blitzecdn.composition.processes`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from blitzecdn import ansible as core_ansible
from blitzecdn.capabilities.cache.service import CacheService
from blitzecdn.capabilities.cache.wiring import build_cache_service
from blitzecdn.capabilities.deployments.ports import (
    DeploymentLocker,
    DeploymentRequirements,
)
from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.capabilities.deployments.wiring import build_deployment_service
from blitzecdn.capabilities.dns import DnsService
from blitzecdn.capabilities.dns.ports import SiteReader
from blitzecdn.capabilities.dns.service import HostService, RuleService
from blitzecdn.capabilities.dns.wiring import (
    build_dns_service,
    build_host_service,
    build_rule_service,
)
from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.edges.wiring import build_edge_operations_service
from blitzecdn.capabilities.jobs.service import JobQueue, JobRunner, JobScheduler
from blitzecdn.capabilities.maintenance import MaintenanceService
from blitzecdn.capabilities.maintenance.wiring import build_maintenance_service
from blitzecdn.capabilities.origins.adapters import OriginCheckPlaybook
from blitzecdn.capabilities.origins.service.checks import OriginCheckService
from blitzecdn.capabilities.origins.wiring import build_origin_check_service
from blitzecdn.capabilities.releases.service import ReleaseService
from blitzecdn.capabilities.releases.wiring import build_release_service
from blitzecdn.capabilities.tls.automatic_ssl.service import AutomaticSslService
from blitzecdn.capabilities.tls.certificates.adapters import CertificateStore
from blitzecdn.capabilities.tls.certificates.ports import (
    WorkflowCoordinator as CertificateWorkflows,
)
from blitzecdn.capabilities.tls.certificates.service import CertificateService
from blitzecdn.capabilities.tls.settings import CertificateConfig
from blitzecdn.capabilities.tls.wiring import (
    build_automatic_ssl_service,
    build_certificate_service,
)
from blitzecdn.capabilities.workflows.adapters.persistence import WorkflowStore
from blitzecdn.capabilities.workflows.service import WorkflowCoordinator
from blitzecdn.composition.infrastructure import Infrastructure
from blitzecdn.composition.roster import (
    OperationInputs,
    RenderingInputs,
    bind_operations,
    bind_renderings,
)
from blitzecdn.core.ansible.rendering import EdgeRenderer
from blitzecdn.core.config import Settings
from blitzecdn.core.features import (
    FeatureRegistry,
    HealthCheck,
    Operations,
    Rendering,
    ScheduledJob,
)
from blitzecdn.core.features.resolution import ResolvedCapabilityEnvironment
from blitzecdn.core.persistence.configuration import AnsibleSettingStore
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import (
    AuditTrail,
    EventRecorder,
    PlaybookRunner,
)

__all__ = ["ApplicationServices", "build_services"]


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ApplicationServices:
    """Every service, and the infrastructure they were built on."""

    infrastructure: Infrastructure
    #: The read side of the audit trail. Entry layers get this rather than
    #: `events`, so they cannot record an event no service performed.
    audit: AuditTrail
    events: EventRecorder
    transactions: UnitOfWork
    deployment_requirements: DeploymentRequirements
    deployment_lock: DeploymentLocker
    fleet: PlaybookRunner
    workflows: WorkflowCoordinator
    workflow_history: WorkflowStore
    dns: DnsService
    site_editor: HostService
    sites: SiteReader
    rules: RuleService
    rendering: Rendering
    releases: ReleaseService
    deployments: DeploymentService
    edges: EdgeOperationsService
    cache: CacheService
    origins: OriginCheckService
    certificate_config: CertificateConfig
    certificates: CertificateService
    automatic_ssl: AutomaticSslService
    operations: Operations
    maintenance: MaintenanceService
    job_runner: JobRunner

    # -- what entry layers read from the infrastructure ------------------

    @property
    def settings(self) -> Settings:
        return self.infrastructure.settings

    @property
    def features(self) -> FeatureRegistry:
        return self.infrastructure.features

    @property
    def capability_config(self) -> ResolvedCapabilityEnvironment:
        return self.infrastructure.capability_config

    @property
    def ansible_settings(self) -> AnsibleSettingStore:
        return self.infrastructure.repository.ansible_settings

    @property
    def certificate_store(self) -> CertificateStore:
        return self.infrastructure.certificate_store

    @property
    def job_queue(self) -> JobQueue:
        return self.infrastructure.job_queue

    @property
    def job_scheduler(self) -> JobScheduler:
        return self.infrastructure.job_scheduler

    @property
    def jobs(self) -> Mapping[str, ScheduledJob]:
        """Every contributed scheduled job, by the name the queue carries."""
        return self.operations.scheduled_jobs

    @property
    def health_checks(self) -> tuple[HealthCheck, ...]:
        """Every reason the shipped features have to call this node unhealthy."""
        return self.operations.health_checks

    def queue_ready(self) -> bool:
        """Whether the durable work queue can be read right now.

        A separate question from "is the database up": a schema that has not
        been migrated has a database and no ``jobs`` table, and a controller in
        that state accepts deployments it can never run.
        """
        return self.infrastructure.queue_ready()

    def close(self) -> None:
        """Release the infrastructure this graph opened."""
        self.infrastructure.close()


def build_services(infrastructure: Infrastructure) -> ApplicationServices:
    """Build every service, each handed exactly the collaborators it declared."""
    infra = infrastructure
    settings = infra.settings
    store = infra.repository
    events = store.audit_log
    clock = infra.clock
    workflows = WorkflowCoordinator(
        journal=store.workflows,
        uow=store,
        retention=settings.history_retention,
        clock=clock,
    )

    # DNS owns canonical zones, rules, records, and their host projection.
    dns = build_dns_service(
        zones=store.zones, rules=store.rules, events=events, uow=store
    )
    site_editor = build_host_service(
        zones=store.zones, rules=store.rules, events=events, uow=store
    )
    # A rule may consult the zone policy it overrides and may never write one.
    rules = build_rule_service(
        rules=store.rules, zones=store.zones, events=events, uow=store
    )

    # What every feature renders into a release, bound from configuration and
    # adapters only, before the compiler that consumes it.
    rendering = infra.features.bind_rendering(
        bind_renderings(
            RenderingInputs(
                capability_config=infra.capability_config,
                certificate_store=infra.certificate_store,
            )
        )
    )
    releases = build_release_service(
        state=store,
        releases=store.releases,
        edges=infra.edges,
        contributors=rendering,
        objections=rendering.site_objections,
        capabilities=tuple(infra.features.capabilities),
        allow_empty_sites=settings.allow_empty_sites,
        retention=settings.history_retention,
        uow=store,
        # The shipped templates and defaults, from the package itself, whatever
        # directory its playbooks run from.
        renderer=EdgeRenderer(
            ansible_dir=core_ansible.ROLES_PATH.parent,
            contributions=infra.edge_contributions,
        ),
    )
    deployments = build_deployment_service(
        settings=settings,
        deployments=store.deployments,
        zones=store.zones,
        rules=store.rules,
        fleet_settings=store.ansible_settings,
        requirements=store.deployment_requirements,
        releases=releases,
        targets=store.deployment_targets,
        edges=infra.edges,
        verifier=infra.serving_probe,
        runner=infra.runner,
        background=infra.background,
        dns=dns,
        workflows=workflows,
        events=events,
        uow=store,
        clock=clock,
    )
    edges = build_edge_operations_service(
        edges=infra.edges, runner=infra.runner, events=events, uow=store
    )
    cache = build_cache_service(
        sites=site_editor, events=events, fleet=infra.runner, clock=clock
    )
    origins = build_origin_check_service(
        sites=site_editor,
        events=events,
        fleet=infra.runner,
        origin_probe=infra.origin_probe,
        clock=clock,
    )
    certificate_config = CertificateConfig.from_capability_config(
        infra.capability_config.for_feature("certificates")
    )
    certificates = build_certificate_service(
        settings=settings,
        config=certificate_config,
        store=infra.certificate_store,
        releases=releases,
        sites=site_editor,
        edges=infra.edges,
        origin_probe=infra.origin_probe,
        requirements=store.deployment_requirements,
        lock=infra.runner,
        events=events,
        dns=dns,
        site_editor=site_editor,
        deployments=deployments,
        # ``@contextmanager`` exposes its concrete private context-manager
        # type to mypy. The behavior is the narrow protocol TLS declares.
        workflows=cast(CertificateWorkflows, workflows),
        uow=store,
        clock=clock,
    )
    automatic_ssl = build_automatic_ssl_service(
        sites=site_editor,
        origin_checks=OriginCheckPlaybook(infra.runner),
        origin_probe=infra.origin_probe,
        site_editor=site_editor,
        deployments=deployments,
    )

    # Contributed jobs close over services, so they are bound once every
    # service exists.
    operations = infra.features.bind_operations(
        bind_operations(
            OperationInputs(
                settings=settings,
                deployments=deployments,
                certificates=certificates,
                automatic_ssl=automatic_ssl,
                certificate_config=certificate_config,
                read_database=lambda: store.workflows.list_workflows(1),
                queue_ready=infra.queue_ready,
            )
        )
    )
    maintenance = build_maintenance_service(
        jobs=operations.scheduled_jobs,
        deployments=deployments,
    )
    return ApplicationServices(
        infrastructure=infra,
        audit=store.audit_log,
        events=events,
        transactions=store,
        deployment_requirements=store.deployment_requirements,
        deployment_lock=infra.runner,
        fleet=infra.runner,
        workflows=workflows,
        workflow_history=store.workflows,
        dns=dns,
        site_editor=site_editor,
        sites=site_editor,
        rules=rules,
        rendering=rendering,
        releases=releases,
        deployments=deployments,
        edges=edges,
        cache=cache,
        origins=origins,
        certificate_config=certificate_config,
        certificates=certificates,
        automatic_ssl=automatic_ssl,
        operations=operations,
        maintenance=maintenance,
        job_runner=JobRunner(
            queue=infra.job_queue,
            scheduler=infra.job_scheduler,
            poll_seconds=settings.worker_poll_seconds,
            clock=clock,
        ),
    )
