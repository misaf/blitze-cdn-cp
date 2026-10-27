"""Converge a compiled release onto the fleet, and record what happened.

DeploymentService owns the lock, the workflow record and finalization; it does
not decide what the fleet serves. It asks the releases capability for a
compiled release and publishes that artifact, so what Ansible read stays
readable afterwards.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from blitzecdn.capabilities.deployments.domain import (
    DEPLOYMENT_WORKFLOW,
    ROLLBACK_WORKFLOW,
    Deployment,
    DeploymentRequirementKind,
    DeploymentStatus,
    DeploymentTarget,
    DriftReport,
    aborted_run,
)
from blitzecdn.capabilities.deployments.ports import (
    DeploymentRequirements,
    DeploymentRunner,
    DeploymentStore,
    DeploymentTargets,
    EdgeAddresses,
    EventRecorder,
    LogReader,
    QueueBackgroundRunner,
    Releases,
    RuleRestore,
    SettingRestore,
    UnitOfWork,
    Workflows,
    YamlWriter,
    ZoneEditor,
    ZoneStore,
)
from blitzecdn.capabilities.deployments.service import reporting
from blitzecdn.capabilities.deployments.service import (
    requirements as requirement_policy,
)
from blitzecdn.capabilities.deployments.service import rollback as rollback_policy
from blitzecdn.capabilities.deployments.service.rollout import Rollout, RolloutOutcome
from blitzecdn.capabilities.deployments.service.validation import DeploymentValidation
from blitzecdn.capabilities.releases.domain import Release
from blitzecdn.capabilities.workflows.domain import WorkflowKind
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.domain.validation import validate_edge_limit
from blitzecdn.core.exceptions import (
    ConfigurationError,
    DeploymentBusyError,
    ExecutionError,
)
from blitzecdn.core.ports.clock import Clock, system_clock

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeploymentPolicy:
    """Configuration owned by deployment workflows."""

    run_dir: Path
    generated_vars_path: Path
    output_limit_bytes: int
    history_retention: int
    runtime_errors: Callable[[], list[str]]


@dataclass(frozen=True)
class DeploymentPersistence:
    """State capabilities changed together by deployment workflows."""

    deployments: DeploymentStore
    #: Per-edge rollout progress, written by the rollout and read by reporting.
    targets: DeploymentTargets
    zones: ZoneStore
    #: Only a rollback writes here, and only wholesale.
    rules: RuleRestore
    #: The same, for the fleet settings a release was converged with.
    settings: SettingRestore
    uow: UnitOfWork
    requirements: DeploymentRequirements


@dataclass(frozen=True)
class DeploymentExecution:
    """Collaborators that publish, launch, and observe deployment work."""

    runner: DeploymentRunner
    background: QueueBackgroundRunner
    read_log: LogReader
    #: Writes the release artifact atomically; Ansible reads it moments later.
    write_yaml: YamlWriter
    #: The per-edge state machine: ordering, fencing and failure meaning. This
    #: service owns the lock, transaction and record around it.
    rollout: Rollout
    #: Which edges a run reaches, and where each answers from outside.
    addresses: EdgeAddresses


class DeploymentService:
    """Runs Ansible against a recorded release and owns the deployment lock.

    The only way the API, CLI, scheduler and worker reach convergence.
    """

    def __init__(
        self,
        *,
        policy: DeploymentPolicy,
        persistence: DeploymentPersistence,
        execution: DeploymentExecution,
        events: EventRecorder,
        dns: ZoneEditor,
        releases: Releases,
        workflows: Workflows,
        clock: Clock = system_clock,
    ) -> None:
        self.clock = clock
        self.policy = policy
        self.persistence = persistence
        self.execution = execution
        self.events = events
        #: The whole of what this service knows about configuration.
        self.releases = releases
        #: Canonical DNS validation, also passed to DeploymentValidation.
        self.dns = dns
        self.workflows = workflows
        #: Built here: every collaborator it needs is already this service's.
        self._validation = DeploymentValidation(
            runtime_errors=policy.runtime_errors,
            dns=dns,
            releases=releases,
            runner=execution.runner,
            write_yaml=execution.write_yaml,
            read_log=execution.read_log,
            run_dir=policy.run_dir,
            output_limit_bytes=policy.output_limit_bytes,
        )

    def initialize(self) -> int:
        """Recover durable work a previous controller process left in flight.

        A held lock means another process is converging, so nothing is recovered.
        Otherwise unfinished deployments are republished with status and checkpoints
        intact; expired claims resume through `run_queued`.
        """
        try:
            with self.execution.runner.lock():
                interrupted = self.persistence.deployments.running_deployments()
                self.workflows.reconcile_interrupted()
                # Re-deliver admitted work that lost its queue entry.
                for deployment in [
                    *interrupted,
                    *self.persistence.deployments.queued_deployments(),
                ]:
                    self.execution.background.enqueue(deployment.id)
                return len(interrupted)
        except DeploymentBusyError:
            _LOGGER.info(
                "another process holds the deployment lock; leaving in-flight "
                "deployments alone"
            )
            return 0

    # -- Validation ----------------------------------------------------

    def validate(self, *, host_limit: str | None = None) -> list[str]:
        """Whether desired state is coherent and the play parses; takes no lock."""
        return self._validation.errors(host_limit=host_limit)

    # -- Deploying -----------------------------------------------------

    def deploy(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment:
        """Converge the edges, returning once the run has finished.

        ``host_limit`` makes it a canary, recorded because success then means only the
        named edges converged.
        """

        with self.execution.runner.lock():
            return self._journalled(
                DEPLOYMENT_WORKFLOW,
                operator,
                None,
                "converged",
                lambda: self.converge(
                    self._queue(
                        operator,
                        check=check,
                        host_limit=host_limit,
                    ),
                    operator,
                ),
            )

    def _journalled(
        self,
        kind: WorkflowKind,
        operator: str,
        resource_id: str | None,
        checkpoint: str,
        work: Callable[[], Deployment],
    ) -> Deployment:
        """Converge inside a workflow record, so every path leaves a durable trace."""
        with self.workflows.run(kind, operator, resource_id) as progress:
            deployment = work()
            progress.checkpoint(checkpoint, {"deployment_id": deployment.id})
            if deployment.status is not DeploymentStatus.SUCCEEDED:
                progress.fail(deployment.detail or deployment.status.value)
            return deployment

    def submit_deployment(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment:
        """Queue a convergence for the worker; callers poll for the outcome."""
        with self.execution.runner.lock():
            return self._queue(
                operator, check=check, host_limit=host_limit, enqueue=True
            )

    def converge_requirements(self, operator: str) -> Deployment | None:
        """Queue one fleet deployment for pending requirements, or ``None`` if none.

        A requirement clears only when a deployment that can satisfy it succeeds, so an
        already queued one is returned rather than a second queued.
        """
        requested_at = self.persistence.requirements.requested_at(
            DeploymentRequirementKind.CERTIFICATES
        )
        if requested_at is None:
            return None
        with self.execution.runner.lock():
            waiting = requirement_policy.queued_to_satisfy(
                self.persistence.deployments.queued_deployments(), requested_at
            )
            if waiting is not None:
                return waiting
            return self._queue(operator, check=False, enqueue=True)

    def rollback(
        self, operator: str, deployment_id: str | None = None, *, check: bool = False
    ) -> Deployment:
        """Converge a prior release and adopt it as canonical desired state.

        No host limit: adoption rewrites canonical records, so a partial rollback
        would leave the control plane asserting a state most edges never got.
        """

        with self.execution.runner.lock():
            return self._journalled(
                ROLLBACK_WORKFLOW,
                operator,
                deployment_id,
                "converged_and_adopted",
                lambda: self.converge(
                    self._queue_rollback(operator, deployment_id, check=check),
                    operator,
                ),
            )

    def submit_rollback(
        self, operator: str, deployment_id: str | None = None, *, check: bool = False
    ) -> Deployment:
        """Queue a rollback for the worker and return the queued record."""
        with self.execution.runner.lock():
            return self._queue_rollback(
                operator, deployment_id, check=check, enqueue=True
            )

    def run_queued(self, deployment_id: str) -> Deployment:
        """Resume durable progress under the exclusive deployment lock.

        A RUNNING row with a free lock belongs to a worker that stopped; its completed
        phases stand, and the rest resume under a new generation.
        """
        with self.execution.runner.lock():
            deployment = self.persistence.deployments.get_deployment(deployment_id)
            if deployment.status not in (
                DeploymentStatus.QUEUED,
                DeploymentStatus.RUNNING,
            ):
                return deployment
            if deployment.status is DeploymentStatus.RUNNING:
                self.workflows.reconcile_interrupted(resource_id=deployment.id)
                self.events.record(
                    domain_event(
                        deployment.operator,
                        "deployment.resumed",
                        "deployment",
                        deployment.id,
                    )
                )
            kind = ROLLBACK_WORKFLOW if deployment.rollback_of else DEPLOYMENT_WORKFLOW
            checkpoint = (
                "converged_and_adopted" if deployment.rollback_of else "converged"
            )
            return self._journalled(
                kind,
                deployment.operator,
                deployment.id,
                checkpoint,
                lambda: self.converge(deployment, deployment.operator),
            )

    # -- Drift ---------------------------------------------------------

    def check_drift(
        self, operator: str, *, host_limit: str | None = None
    ) -> DriftReport:
        """Ask the fleet, in check mode, whether it still matches desired state."""
        deployment = self.deploy(operator, check=True, host_limit=host_limit)
        report = self.drift_report(deployment.id)
        self.events.record(
            domain_event(
                operator,
                "drift.checked",
                "deployment",
                deployment.id,
                {
                    "in_sync": report.in_sync,
                    "drifted": [host.host for host in report.drifted],
                    "unreachable": [host.host for host in report.unreachable],
                },
            )
        )
        return report

    def drift_report(self, deployment_id: str) -> DriftReport:
        """Read a recorded check-mode run as a drift report."""
        return reporting.drift_report(
            self.persistence.deployments, self.persistence.targets, deployment_id
        )

    # -- History -------------------------------------------------------

    def get_deployment(self, deployment_id: str) -> Deployment:
        """One deployment, for an operator or a client polling a queued run."""
        return self.persistence.deployments.get_deployment(deployment_id)

    def paginate_deployments(
        self, *, limit: int, offset: int
    ) -> PageResult[Deployment]:
        return self.persistence.deployments.paginate_deployments(
            limit=limit, offset=offset
        )

    def list_deployments(self, limit: int = 20) -> list[Deployment]:
        """Recent deployments, newest first."""
        return self.persistence.deployments.list_deployments(limit)

    def paginate_targets(
        self, deployment_id: str, *, limit: int, offset: int
    ) -> PageResult[DeploymentTarget]:
        self.get_deployment(deployment_id)
        return self.persistence.targets.paginate_targets(
            deployment_id, limit=limit, offset=offset
        )

    def targets(self, deployment_id: str) -> Sequence[DeploymentTarget]:
        """How far each edge got in this deployment."""
        return tuple(self.persistence.targets.targets(deployment_id))

    def site_is_deployed(self, site_name: str) -> bool:
        """Whether the most recent real deployment carried this site."""
        return reporting.site_is_deployed(
            self.persistence.deployments, self.releases, site_name
        )

    # -- Internals -----------------------------------------------------

    def _queue(
        self,
        operator: str,
        *,
        check: bool,
        release_id: str | None = None,
        rollback_of: str | None = None,
        host_limit: str | None = None,
        canonical_digest: str | None = None,
        enqueue: bool = False,
    ) -> Deployment:
        """Record a QUEUED deployment. Callers must hold the deployment lock.

        A forward deploy compiles current state here, under the lock, so the release
        named is the state when admitted. ``release_id`` is given only by a rollback.
        The job, deployment and targets are written in one transaction, so a refused
        enqueue leaves nothing stranded.
        """
        # Normalised before storing, so a malformed limit is refused before any row.
        limit = validate_edge_limit(host_limit)
        release = (
            self.releases.get(release_id)
            if release_id is not None
            else self.releases.prepare(host_limit=limit)
        )
        with self.persistence.uow.transaction():
            deployment = self.persistence.deployments.create_deployment(
                operator,
                check_mode=check,
                rollback_of=rollback_of,
                release_id=release.id,
                host_limit=limit,
                canonical_digest=canonical_digest,
            )
            # The target set is fixed at admission.
            planned_edges = (
                self.execution.addresses.selected(None)
                if rollback_of is not None
                else [target.name for target in release.targets]
            )
            self.persistence.targets.plan(deployment.id, planned_edges, fence=0)
            # Pruned inline by a writer, so retention cannot silently stop.
            self.persistence.deployments.prune_history(self.policy.history_retention)
            self.events.record(
                domain_event(
                    operator,
                    "deployment.queued",
                    "deployment",
                    deployment.id,
                    {
                        "check_mode": check,
                        "rollback_of": rollback_of,
                        "host_limit": limit,
                        "release_id": release.id,
                    },
                )
            )
            if enqueue:
                self.execution.background.enqueue(deployment.id)
        return deployment

    def _queue_rollback(
        self,
        operator: str,
        deployment_id: str | None,
        *,
        check: bool,
        enqueue: bool = False,
    ) -> Deployment:
        target = rollback_policy.select_target(
            self.persistence.deployments, self.releases, deployment_id
        )
        return self._queue(
            operator,
            check=check,
            release_id=self.persistence.deployments.deployment_release(target.id),
            rollback_of=target.id,
            # Canonical state now. Record writes take no deployment lock, so adoption
            # compares against this and refuses rather than silently delete a record
            # written during the rollback.
            canonical_digest=self.releases.inputs().digest,
            enqueue=enqueue,
        )

    def converge(self, deployment: Deployment, operator: str) -> Deployment:
        """Roll this deployment's release out edge by edge. Callers hold the lock.

        The lock stops two deployments overlapping; the rollout's fence stops a worker
        paused past its lease from recording results for a rollout taken over since.
        """
        check = deployment.check_mode
        if deployment.status is DeploymentStatus.QUEUED:
            deployment = self.persistence.deployments.save(
                deployment, deployment.start(now=self.clock())
            )
        try:
            release = self.releases.get(deployment.release_id)
            planned_edges = [
                target.edge
                for target in self.persistence.targets.targets(deployment.id)
            ]
            missing = set(planned_edges) - set(self.execution.addresses.selected(None))
            if missing:
                raise ConfigurationError(
                    "deployment targets were removed after admission: "
                    + ", ".join(sorted(missing))
                )
            outcome = self.execution.rollout.converge(
                deployment.id,
                release,
                edges=planned_edges,
                check=check,
            )
            deployment = self._complete_rollout(
                deployment, outcome, release, operator, check=check
            )
        except BaseException as exc:
            deployment, interrupted = self._fail_convergence(deployment, operator, exc)
            if interrupted:
                raise
            if isinstance(exc, ExecutionError):
                raise
            return deployment
        return deployment

    def _complete_rollout(
        self,
        deployment: Deployment,
        outcome: RolloutOutcome,
        release: Release,
        operator: str,
        *,
        check: bool,
    ) -> Deployment:
        """Commit a rollout's result and atomically adopt a successful rollback.

        Status comes from the rollout, not any one Ansible result: an unattempted edge
        makes an otherwise clean run a failure.
        """
        # No Ansible result means either a failure before anything ran, or an empty
        # fleet; the second is a success with an explanation.
        run = outcome.run or aborted_run(
            RuntimeError(
                outcome.error
                or "no edges are registered, so nothing was converged; add one "
                "with 'blitzecdn edge add'"
            ),
            interrupted=False,
            now=self.clock(),
        )
        if outcome.succeeded:
            target_status = DeploymentStatus.SUCCEEDED
        elif outcome.run is not None:
            # The run's own reading keeps a timeout a timeout, but a clean run inside a
            # stopped rollout is still a failure.
            target_status = DeploymentStatus.of(outcome.run)
            if target_status is DeploymentStatus.SUCCEEDED:
                target_status = DeploymentStatus.FAILED
        else:
            target_status = DeploymentStatus.FAILED
        adopts_rollback = bool(
            deployment.rollback_of
            and target_status is DeploymentStatus.SUCCEEDED
            and not check
        )
        with self.persistence.uow.transaction():
            if adopts_rollback:
                rollback_policy.require_unchanged_canonical(self.releases, deployment)
                rollback_policy.adopt_inputs(
                    self.persistence.zones,
                    self.persistence.rules,
                    self.persistence.settings,
                    self.releases.inputs_for(release.id),
                )
            deployment = self.persistence.deployments.save(
                deployment,
                deployment.finish(
                    target_status, now=self.clock(), result=run, error=outcome.error
                ),
            )
            if target_status is DeploymentStatus.SUCCEEDED:
                requirement_policy.settle(self.persistence.requirements, deployment)
            self.events.record(
                domain_event(
                    operator,
                    f"deployment.{deployment.status}",
                    "deployment",
                    deployment.id,
                    {
                        "return_code": run.return_code,
                        "release_id": release.id,
                        "converged": list(outcome.converged),
                        "unattempted": list(outcome.unattempted),
                        "error": outcome.error,
                    },
                )
            )
            if adopts_rollback:
                self.events.record(
                    domain_event(
                        operator,
                        "rollback.applied",
                        "deployment",
                        deployment.id,
                        {"target": deployment.rollback_of},
                    )
                )
        return deployment

    def _fail_convergence(
        self, deployment: Deployment, operator: str, exc: BaseException
    ) -> tuple[Deployment, bool]:
        """Finalize a convergence that ended before producing a usable result."""
        interrupted = not isinstance(exc, Exception)
        status = DeploymentStatus.ABANDONED if interrupted else DeploymentStatus.FAILED
        with self.persistence.uow.transaction():
            now = self.clock()
            deployment = self.persistence.deployments.save(
                deployment,
                deployment.finish(
                    status,
                    now=now,
                    result=aborted_run(exc, interrupted=interrupted, now=now),
                ),
            )
            self.events.record(
                domain_event(
                    operator,
                    "deployment.abandoned" if interrupted else "deployment.failed",
                    "deployment",
                    deployment.id,
                    {"error_type": type(exc).__name__},
                )
            )
        return deployment, interrupted

    def publish_artifact(self, release: Release, path: Path) -> None:
        """Put a release's artifact, plus its digest, where Ansible reads it."""
        self.execution.write_yaml(path, release.execution_document())
