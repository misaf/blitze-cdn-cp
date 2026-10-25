"""What each process is given, and what it does when it starts and stops.

Every container here is built from the one service graph in
:mod:`blitzecdn.composition.services` and holds only what its process uses.
Delivery code reads its own container, so an API route cannot reach a service
the API was not given, and a process's differences are which container it
builds rather than branches inside one object.

See docs/decisions/0021-capability-roster-and-process-composition.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from blitzecdn.capabilities.cache.service import CacheService
from blitzecdn.capabilities.deployments.domain import (
    TERMINAL_STATUSES,
    DeploymentStatus,
)
from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.capabilities.dns import DnsService
from blitzecdn.capabilities.dns.ports import SiteReader
from blitzecdn.capabilities.dns.service import RuleService
from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.jobs.composition import schedule_specs
from blitzecdn.capabilities.jobs.domain import DeferredJobError, PermanentJobError
from blitzecdn.capabilities.jobs.ports import JobHandler
from blitzecdn.capabilities.jobs.service import (
    DEPLOYMENT_JOB,
    SCHEDULED_JOB,
    JobQueue,
    JobRunner,
    JobScheduler,
)
from blitzecdn.capabilities.maintenance import MaintenanceService
from blitzecdn.capabilities.origins.service.checks import OriginCheckService
from blitzecdn.capabilities.releases.service import ReleaseService
from blitzecdn.capabilities.tls.automatic_ssl.service import AutomaticSslService
from blitzecdn.capabilities.tls.certificates.service import CertificateService
from blitzecdn.capabilities.tls.config import CertificateConfig
from blitzecdn.capabilities.workflows.adapters.persistence import WorkflowStore
from blitzecdn.composition.infrastructure import Infrastructure
from blitzecdn.composition.services import ApplicationServices
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import DeploymentBusyError
from blitzecdn.core.persistence.configuration import AnsibleSettingStore
from blitzecdn.core.plugins import (
    HealthCheck,
    Operations,
    PluginRegistry,
    ScheduledJob,
)
from blitzecdn.core.plugins.resolution import ResolvedCapabilityEnvironment
from blitzecdn.core.ports.operations import AuditTrail, PlaybookRunner

__all__ = [
    "ApiServices",
    "CliServices",
    "HandoffServices",
    "WorkerServices",
    "build_api",
    "build_cli",
    "build_install_handoff",
    "build_worker",
]


@dataclass(frozen=True, slots=True)
class ApiServices:
    """What the HTTP API's routes call, and the node's startup duties."""

    settings: Settings
    audit: AuditTrail
    sites: SiteReader
    dns: DnsService
    rules: RuleService
    releases: ReleaseService
    deployments: DeploymentService
    edges: EdgeOperationsService
    cache: CacheService
    origins: OriginCheckService
    certificate_config: CertificateConfig
    certificates: CertificateService
    automatic_ssl: AutomaticSslService
    workflow_history: WorkflowStore
    health_checks: tuple[HealthCheck, ...]
    _operations: Operations
    _scheduler: JobScheduler
    _jobs: Mapping[str, ScheduledJob]
    _infrastructure: Infrastructure

    def start(self) -> None:
        """Run every feature's node startup, then publish the schedules.

        The API is the process that lives as long as the node does, so
        recovery work belongs to it. Registration is idempotent, never moves
        an existing schedule's next firing, and drops a schedule nothing
        contributes any more.
        """
        self._operations.startup()
        self._scheduler.register(schedule_specs(self._jobs))

    def stop(self) -> None:
        self._operations.shutdown()

    def close(self) -> None:
        self._infrastructure.close()


@dataclass(frozen=True, slots=True)
class WorkerServices:
    """The durable job loop, with every job kind's handler installed."""

    job_runner: JobRunner
    job_scheduler: JobScheduler
    _operations: Operations
    _jobs: Mapping[str, ScheduledJob]
    _infrastructure: Infrastructure

    def start(self) -> None:
        """Publish the schedules. Recovery is the API's startup, not this."""
        self.job_scheduler.register(schedule_specs(self._jobs))

    def stop(self) -> None:
        self._operations.shutdown()

    def close(self) -> None:
        self._infrastructure.close()


@dataclass(frozen=True, slots=True)
class CliServices:
    """What the command groups call. A CLI invocation starts nothing."""

    settings: Settings
    plugins: PluginRegistry
    capability_config: ResolvedCapabilityEnvironment
    ansible_settings: AnsibleSettingStore
    audit: AuditTrail
    fleet: PlaybookRunner
    sites: SiteReader
    dns: DnsService
    rules: RuleService
    releases: ReleaseService
    deployments: DeploymentService
    edges: EdgeOperationsService
    cache: CacheService
    origins: OriginCheckService
    certificates: CertificateService
    automatic_ssl: AutomaticSslService
    job_queue: JobQueue
    job_scheduler: JobScheduler
    _infrastructure: Infrastructure

    def close(self) -> None:
        self._infrastructure.close()


@dataclass(frozen=True, slots=True)
class HandoffServices:
    """What the installer's handoff needs: the local edge and a first deploy."""

    edges: EdgeOperationsService
    deployments: DeploymentService
    _infrastructure: Infrastructure

    def close(self) -> None:
        self._infrastructure.close()


def build_api(services: ApplicationServices) -> ApiServices:
    return ApiServices(
        settings=services.settings,
        audit=services.audit,
        sites=services.sites,
        dns=services.dns,
        rules=services.rules,
        releases=services.releases,
        deployments=services.deployments,
        edges=services.edges,
        cache=services.cache,
        origins=services.origins,
        certificate_config=services.certificate_config,
        certificates=services.certificates,
        automatic_ssl=services.automatic_ssl,
        workflow_history=services.workflow_history,
        health_checks=services.health_checks,
        _operations=services.operations,
        _scheduler=services.job_scheduler,
        _jobs=services.jobs,
        _infrastructure=services.infrastructure,
    )


def build_worker(services: ApplicationServices) -> WorkerServices:
    """The worker's container. Only this installs the queue's handlers."""
    services.job_queue.consume_with(
        _job_handlers(services.deployments, services.maintenance)
    )
    return WorkerServices(
        job_runner=services.job_runner,
        job_scheduler=services.job_scheduler,
        _operations=services.operations,
        _jobs=services.jobs,
        _infrastructure=services.infrastructure,
    )


def build_cli(services: ApplicationServices) -> CliServices:
    return CliServices(
        settings=services.settings,
        plugins=services.plugins,
        capability_config=services.capability_config,
        ansible_settings=services.ansible_settings,
        audit=services.audit,
        fleet=services.fleet,
        sites=services.sites,
        dns=services.dns,
        rules=services.rules,
        releases=services.releases,
        deployments=services.deployments,
        edges=services.edges,
        cache=services.cache,
        origins=services.origins,
        certificates=services.certificates,
        automatic_ssl=services.automatic_ssl,
        job_queue=services.job_queue,
        job_scheduler=services.job_scheduler,
        _infrastructure=services.infrastructure,
    )


def build_install_handoff(services: ApplicationServices) -> HandoffServices:
    return HandoffServices(
        edges=services.edges,
        deployments=services.deployments,
        _infrastructure=services.infrastructure,
    )


def _job_handlers(
    deployments: DeploymentService, maintenance: MaintenanceService
) -> dict[str, JobHandler]:
    """What each kind of job does in the worker.

    Both handlers call a service already built for duplicate delivery:
    ``run_queued`` leaves terminal deployments untouched and resumes unfinished
    ones; maintenance runs are idempotent. The deployment's persisted outcome,
    including on redelivery, determines whether its job succeeded.
    """
    failed_states = TERMINAL_STATUSES - {DeploymentStatus.SUCCEEDED}

    def deployment(payload: Mapping[str, Any]) -> None:
        deployment_id = str(payload["deployment_id"])
        try:
            result = deployments.run_queued(deployment_id)
        except DeploymentBusyError as exc:
            raise DeferredJobError(str(exc)) from exc
        except Exception:
            # A runner error may already have finalized the deployment. Retry
            # delivery failures only while no terminal failure is on record.
            result = deployments.get_deployment(deployment_id)
            if result.status not in failed_states:
                raise
        if result.status in failed_states:
            raise PermanentJobError(
                f"deployment {result.id} ended as {result.status}: "
                f"{result.detail or 'the deployment did not succeed'}"
            )
        if result.status is not DeploymentStatus.SUCCEEDED:
            raise RuntimeError(
                f"deployment {result.id} returned without a terminal outcome: "
                f"{result.status}"
            )

    def scheduled(payload: Mapping[str, Any]) -> None:
        maintenance.run(str(payload["job"]))

    return {DEPLOYMENT_JOB: deployment, SCHEDULED_JOB: scheduled}
