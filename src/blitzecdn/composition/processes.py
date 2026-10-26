"""One container per process, and what each process does when it starts and stops.

Every process resolves from the same graph. They differ in what is added to
it: the API's node startup, and the worker's job handlers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from dishka import Container, Provider, Scope, make_container, provide
from dishka.integrations.fastapi import FastapiProvider

from blitzecdn.capabilities.deployments.domain import (
    TERMINAL_STATUSES,
    DeploymentStatus,
)
from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.capabilities.jobs.domain import (
    DONE,
    Defer,
    Done,
    Fail,
    JobOutcome,
    Retry,
)
from blitzecdn.capabilities.jobs.ports import JobHandler
from blitzecdn.capabilities.jobs.service import (
    DEPLOYMENT_JOB,
    SCHEDULED_JOB,
    JobQueue,
    JobRunner,
    JobScheduler,
)
from blitzecdn.capabilities.jobs.wiring import schedule_specs
from blitzecdn.capabilities.maintenance import MaintenanceService
from blitzecdn.composition.infrastructure import Adapters, InfrastructureProvider
from blitzecdn.composition.services import OperationRun, OperationRuns, ServiceProvider
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import BlitzeError, DeploymentBusyError
from blitzecdn.core.features import FeatureRegistry, Operations

__all__ = [
    "ApiNode",
    "ApiProvider",
    "Worker",
    "WorkerProvider",
    "api_container",
    "cli_container",
    "handoff_container",
    "refuse_missing_capabilities",
    "worker_container",
]


@dataclass(frozen=True, slots=True)
class ApiNode:
    """The API process's startup duties."""

    operations: Operations
    scheduler: JobScheduler

    def start(self) -> None:
        """Run every feature's node startup, then register the schedules."""
        self.operations.startup()
        self.scheduler.register(schedule_specs(self.operations.scheduled_jobs))

    def stop(self) -> None:
        self.operations.shutdown()


@dataclass(frozen=True, slots=True)
class Worker:
    """The durable job loop, with every job kind's handler installed."""

    runner: JobRunner
    scheduler: JobScheduler
    operations: Operations

    def start(self) -> None:
        """Publish the schedules. Recovery is the API's startup, not this."""
        self.scheduler.register(schedule_specs(self.operations.scheduled_jobs))

    def stop(self) -> None:
        self.operations.shutdown()


class ApiProvider(Provider):
    scope = Scope.APP

    @provide
    def node(self, operations: Operations, scheduler: JobScheduler) -> ApiNode:
        return ApiNode(operations=operations, scheduler=scheduler)


class WorkerProvider(Provider):
    scope = Scope.APP

    @provide
    def worker(
        self,
        queue: JobQueue,
        runner: JobRunner,
        scheduler: JobScheduler,
        operations: Operations,
        deployments: DeploymentService,
        maintenance: MaintenanceService,
        runs: OperationRuns,
    ) -> Worker:
        """Only this installs the queue's handlers."""
        queue.consume_with(
            {
                **_job_handlers(deployments, maintenance),
                **_operation_handlers(runs),
            }
        )
        return Worker(runner=runner, scheduler=scheduler, operations=operations)


def _container(
    settings: Settings, adapters: Adapters | None, *extra: Provider
) -> Container:
    container = make_container(
        InfrastructureProvider(),
        ServiceProvider(),
        *extra,
        context={Settings: settings, Adapters: adapters or Adapters()},
    )
    return refuse_missing_capabilities(container)


def refuse_missing_capabilities(container: Container) -> Container:
    """Resolve the registry now: the graph is lazy, and a command that needs
    no feature must still refuse an installation missing a required one."""
    try:
        container.get(FeatureRegistry)
    except BaseException:
        container.close()
        raise
    return container


def api_container(settings: Settings, adapters: Adapters | None = None) -> Container:
    """The HTTP API's graph, with its node startup and a scope per request."""
    return _container(settings, adapters, ApiProvider(), FastapiProvider())


def worker_container(settings: Settings, adapters: Adapters | None = None) -> Container:
    return _container(settings, adapters, WorkerProvider())


def cli_container(settings: Settings, adapters: Adapters | None = None) -> Container:
    """What the command groups resolve from. A CLI invocation starts nothing."""
    return _container(settings, adapters)


def handoff_container(
    settings: Settings, adapters: Adapters | None = None
) -> Container:
    """What the installer's handoff resolves: the local edge and a first deploy."""
    return _container(settings, adapters)


def _job_handlers(
    deployments: DeploymentService, maintenance: MaintenanceService
) -> dict[str, JobHandler]:
    """What each job kind does in the worker; both handlers tolerate redelivery."""
    failed_states = TERMINAL_STATUSES - {DeploymentStatus.SUCCEEDED}

    def deployment(payload: Mapping[str, Any]) -> JobOutcome:
        deployment_id = str(payload["deployment_id"])
        try:
            result = deployments.run_queued(deployment_id)
        except DeploymentBusyError as exc:
            return Defer(str(exc))
        except Exception:
            # A runner error may already have finalized the deployment. Retry
            # delivery failures only while no terminal failure is on record.
            result = deployments.get_deployment(deployment_id)
            if result.status not in failed_states:
                raise
        if result.status in failed_states:
            return Fail(
                f"deployment {result.id} ended as {result.status}: "
                f"{result.detail or 'the deployment did not succeed'}"
            )
        if result.status is not DeploymentStatus.SUCCEEDED:
            return Retry(
                f"deployment {result.id} returned without a terminal outcome: "
                f"{result.status}"
            )
        return DONE

    def scheduled(payload: Mapping[str, Any]) -> JobOutcome:
        maintenance.run(str(payload["job"]))
        return DONE

    return {DEPLOYMENT_JOB: deployment, SCHEDULED_JOB: scheduled}


def _operation_handlers(runs: Mapping[str, OperationRun]) -> dict[str, JobHandler]:
    """Each edge operation as a job: its report is the result, a refusal fails it."""

    def handler(run: OperationRun) -> JobHandler:
        def handle(payload: Mapping[str, Any]) -> JobOutcome:
            try:
                return Done(result=run(payload))
            except BlitzeError as error:
                return Fail(str(error))

        return handle

    return {kind: handler(run) for kind, run in runs.items()}
