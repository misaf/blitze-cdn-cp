"""Convergence, history, rollback, and drift.

Everything here is about turning a stored snapshot into a run of Ansible and
recording what happened. Nothing here decides *what* should be deployed; that
is the zone editor's job, and this service reads its output through
``DnsService.resync_hostnames`` only when a rollback rewrites canonical state.

One service for the convergence paths, deliberately. Deploy, queued deploy,
rollback, drift and recovery look like five use cases, but they are one run of
Ansible reached five ways: each has to take the same cross-process lock, move
the same record through the same transition table, and finalise inside the same
transaction. Splitting *those* into handlers would hand each a copy of the same
four collaborators and leave the lock ordering — the part that is actually
difficult — spread across the pieces rather than stated once.

What is lifted out is everything with its own reason to change that does not
touch the lock, because keeping it here made "must hold the lock" and "must
never take it" neighbours in one class:

* ``service.rollback`` owns what rolling back means.
* ``service.validation`` owns whether desired state could be converged at all —
  asked without the lock, and answered against a scratch file precisely so it
  cannot publish over a deploy in flight.
* ``service.reporting`` owns what a *recorded* run may be read as evidence of,
  which is a rule about stored rows and touches neither Ansible nor the lock.

``domain.aborted_run`` is a pure value. What is left here is the run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from blitzecdn.capabilities.deployments.domain import (
    Deployment,
    DeploymentRequirementKind,
    DeploymentStatus,
    DriftReport,
    aborted_run,
)
from blitzecdn.capabilities.deployments.domain.snapshots import snapshot_digest
from blitzecdn.capabilities.deployments.ports import (
    DeploymentRequirements,
    DeploymentRunner,
    DeploymentStore,
    DesiredStateRenderer,
    EventRecorder,
    LogReader,
    QueueBackgroundRunner,
    SiteRestore,
    SiteValidator,
    UnitOfWork,
    ZoneEditor,
    ZoneStore,
)
from blitzecdn.capabilities.deployments.service import reporting
from blitzecdn.capabilities.deployments.service import rollback as rollback_policy
from blitzecdn.capabilities.deployments.service.validation import DeploymentValidation
from blitzecdn.core.application.workflows import WorkflowCoordinator
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.domain.operations import WorkflowKind
from blitzecdn.core.domain.runs import AnsibleRun
from blitzecdn.core.domain.validation import validate_edge_limit
from blitzecdn.core.exceptions import DeploymentBusyError, ExecutionError

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
    zones: ZoneStore
    #: Only a rollback writes here, and only wholesale.
    sites: SiteRestore
    uow: UnitOfWork
    requirements: DeploymentRequirements


@dataclass(frozen=True)
class DeploymentExecution:
    """Collaborators that render, launch, and observe deployment work."""

    runner: DeploymentRunner
    background: QueueBackgroundRunner
    read_log: LogReader
    renderer: DesiredStateRenderer
    #: What the installed plugins know about a site that should stop a deploy.
    #: A collaborator rather than policy: which plugins are installed is a
    #: composition decision, and this service asks the question without knowing
    #: who answers it.
    validator: SiteValidator


class DeploymentService:
    """Runs Ansible against a recorded snapshot and owns the deployment lock.

    The capability's public face: the API, the CLI, the scheduler and the Dramatiq
    worker all reach convergence through here and through nothing else, which
    is what makes "who may start a deployment" a question with one answer.
    """

    def __init__(
        self,
        *,
        policy: DeploymentPolicy,
        persistence: DeploymentPersistence,
        execution: DeploymentExecution,
        events: EventRecorder,
        dns: ZoneEditor,
        workflows: WorkflowCoordinator,
    ) -> None:
        self.policy = policy
        self.persistence = persistence
        self.execution = execution
        self.events = events
        #: ``resync_hostnames`` on the rollback path, ``validation_errors``
        #: before every run, and nothing else.
        self.dns = dns
        self.workflows = workflows
        #: Built here rather than injected: every collaborator it needs is one
        #: this service was already given, and asking the composition root for
        #: a second object assembled from the same nine would put the fact that
        #: they are the same nine in a place no test of this capability can see.
        self._validation = DeploymentValidation(
            runtime_errors=policy.runtime_errors,
            dns=dns,
            deployments=persistence.deployments,
            validator=execution.validator,
            runner=execution.runner,
            renderer=execution.renderer,
            read_log=execution.read_log,
            run_dir=policy.run_dir,
            output_limit_bytes=policy.output_limit_bytes,
        )

    def initialize(self) -> int:
        """Recover durable work a previous controller process left in flight.

        Only ever under the deployment lock, and this is not a formality. The
        rows this abandons are identified by status alone, so a controller that
        abandoned unconditionally could not tell "left behind by a process that
        died" from "being converged right now by another process" — and the
        second is ordinary here: the API restarts while a CLI ``blitzecdn
        deploy`` is minutes into a run, which is exactly what an upgrade does.
        Abandoning that run rewrites the record of a deployment still changing
        edges, and its own final transition then fails against the status this
        put there, so a run that succeeded ends up recorded as neither.

        A held lock therefore means "someone is deploying", which is the one
        case where there is nothing to recover, so declining is the whole fix.
        The next start with an idle lock does the cleanup.
        """
        try:
            with self.execution.runner.lock():
                recovered = self.persistence.deployments.abandon_running()
                self.workflows.reconcile_interrupted()
                for deployment in self.persistence.deployments.queued_deployments():
                    self.execution.background.enqueue(deployment.id)
                return recovered
        except DeploymentBusyError:
            _LOGGER.info(
                "another process holds the deployment lock; leaving in-flight "
                "deployments alone"
            )
            return 0

    # -- Validation ----------------------------------------------------

    def validate(self) -> list[str]:
        """Answer whether desired state is coherent and the play parses.

        The answer is ``service.validation``'s, and deliberately not reached
        under the lock this service otherwise holds
        for everything: validating is a question about the current desired
        state, and taking the lock to ask it would make ``blitzecdn validate``
        block behind a fleet convergence that has nothing to do with it.
        """
        return self._validation.errors()

    # -- Deploying -----------------------------------------------------

    def deploy(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment:
        """Converge the edges, returning once the run has finished.

        ``host_limit`` narrows the run to some of them — a canary. It is
        recorded on the deployment because it changes what success means: the
        snapshot became reality on the named edges only, and the rest are
        still serving whatever they had.
        """

        with self.execution.runner.lock():
            return self._journalled(
                WorkflowKind.DEPLOYMENT,
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
        """Converge inside a workflow record, whoever asked for the convergence.

        Every path that runs Ansible goes through here, so the durable trace no
        longer depends on which transport was used. It did: the synchronous
        path took a workflow and the queued path did not, which left the run
        that outlives the call that started it — and therefore the one that most
        needs a record surviving a restart — as the one without a record at all.
        """
        with self.workflows.run(kind, operator, resource_id) as progress:
            deployment = work()
            progress.checkpoint(checkpoint, {"deployment_id": deployment.id})
            if deployment.status is not DeploymentStatus.SUCCEEDED:
                progress.fail(deployment.detail or deployment.status.value)
            return deployment

    def submit_deployment(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment:
        """Queue a convergence for a Dramatiq worker and return the queued record.

        A full run can take as long as ``deployment_timeout_seconds``, far
        longer than any HTTP client will wait, so callers poll
        ``GET /v1/deployments/{id}`` for the outcome.
        """
        return self._submit(
            lambda: self._queue(operator, check=check, host_limit=host_limit),
            operator,
        )

    def rollback(
        self, operator: str, deployment_id: str | None = None, *, check: bool = False
    ) -> Deployment:
        """Converge a prior snapshot and adopt it as canonical desired state.

        Deliberately takes no host limit. On success this rewrites the
        canonical records, so a rollback that reached only some edges would
        leave the control plane asserting a state the rest of the fleet has
        never been given — the precise disagreement rollback exists to end.
        """

        with self.execution.runner.lock():
            return self._journalled(
                WorkflowKind.ROLLBACK,
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
        """Queue a rollback for a Dramatiq worker and return the queued record."""
        return self._submit(
            lambda: self._queue_rollback(operator, deployment_id, check=check),
            operator,
        )

    def run_queued(self, deployment_id: str) -> Deployment:
        """Run one durable queue item, ignoring duplicate delivery safely."""
        with self.execution.runner.lock():
            deployment = self.persistence.deployments.get_deployment(deployment_id)
            if deployment.status is not DeploymentStatus.QUEUED:
                return deployment
            kind = (
                WorkflowKind.ROLLBACK
                if deployment.rollback_of
                else WorkflowKind.DEPLOYMENT
            )
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
        """Ask the fleet whether it still matches the declared desired state.

        A check-mode convergence, read as a question rather than a rehearsal.
        Nothing on any edge changes; the run reports what it *would* change,
        and anything it would change is by definition something that drifted
        away from desired state since the last deploy.
        """
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
        return reporting.drift_report(self.persistence.deployments, deployment_id)

    # -- History -------------------------------------------------------

    def get_deployment(self, deployment_id: str) -> Deployment:
        """One deployment, for an operator or a client polling a queued run."""
        return self.persistence.deployments.get_deployment(deployment_id)

    def list_deployments(self, limit: int = 20) -> list[Deployment]:
        """Recent deployments, newest first."""
        return self.persistence.deployments.list_deployments(limit)

    def site_is_deployed(self, site_name: str) -> bool:
        """Whether the most recent real deployment carried this site."""
        return reporting.site_is_deployed(self.persistence.deployments, site_name)

    # -- Internals -----------------------------------------------------

    def _queue(
        self,
        operator: str,
        *,
        check: bool,
        snapshot: str | None = None,
        rollback_of: str | None = None,
        host_limit: str | None = None,
        canonical_digest: str | None = None,
    ) -> Deployment:
        """Record a QUEUED deployment. Callers must hold the deployment lock."""
        # Normalised before it is stored, so the record shows what actually ran
        # rather than what was typed, and a malformed limit is refused before a
        # deployment row exists to explain.
        limit = validate_edge_limit(host_limit)
        with self.persistence.uow.transaction():
            deployment = self.persistence.deployments.create_deployment(
                operator,
                check_mode=check,
                rollback_of=rollback_of,
                snapshot=snapshot,
                host_limit=limit,
                canonical_digest=canonical_digest,
            )
            # Applied by whatever already writes, for the same reason run-log
            # retention lives in the runner: a policy enforced by a timer of
            # its own is one that silently stops being enforced when the unit
            # was never installed.
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
                    },
                )
            )
        return deployment

    def _queue_rollback(
        self, operator: str, deployment_id: str | None, *, check: bool
    ) -> Deployment:
        target = rollback_policy.select_target(
            self.persistence.deployments, deployment_id
        )
        return self._queue(
            operator,
            check=check,
            snapshot=self.persistence.deployments.deployment_snapshot(target.id),
            rollback_of=target.id,
            # What canonical state looks like right now. Adoption compares
            # against this and refuses if a record was written while the
            # rollback was converging — the deployment lock does not exclude
            # record writes, and restoring wholesale over one would delete it
            # with no conflict and nothing left to say it existed.
            canonical_digest=snapshot_digest(self.persistence.deployments.snapshot()),
        )

    def _submit(
        self,
        queue: Callable[[], Deployment],
        operator: str,
    ) -> Deployment:
        """Record durable intent, publish its ID, and return the queued record."""
        with self.execution.runner.lock():
            deployment = queue()
            try:
                self.execution.background.enqueue(deployment.id)
            except BaseException as exc:
                with self.persistence.uow.transaction():
                    self.persistence.deployments.transition(
                        deployment.id,
                        DeploymentStatus.QUEUED,
                        DeploymentStatus.FAILED,
                        finished_at=datetime.now(UTC),
                        result=aborted_run(exc, interrupted=False),
                    )
                    self.events.record(
                        domain_event(
                            operator,
                            "deployment.failed",
                            "deployment",
                            deployment.id,
                            {"error_type": type(exc).__name__},
                        )
                    )
                raise
        return deployment

    def converge(self, deployment: Deployment, operator: str) -> Deployment:
        """Run Ansible for a queued deployment. Callers must hold the lock."""
        check = deployment.check_mode
        deployment = self.persistence.deployments.transition(
            deployment.id,
            DeploymentStatus.QUEUED,
            DeploymentStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        try:
            snapshot = self.persistence.deployments.deployment_snapshot(deployment.id)
            self.write_desired_state(snapshot, self.policy.generated_vars_path)
            run = self.execution.runner.run(
                check=check, host_limit=deployment.host_limit
            )
            deployment = self._complete_run(
                deployment, run, snapshot, operator, check=check
            )
        except BaseException as exc:
            deployment, interrupted = self._fail_convergence(deployment, operator, exc)
            if interrupted:
                raise
            if isinstance(exc, ExecutionError):
                raise
            return deployment
        return deployment

    def _complete_run(
        self,
        deployment: Deployment,
        run: AnsibleRun,
        snapshot: str,
        operator: str,
        *,
        check: bool,
    ) -> Deployment:
        """Commit a runner result and atomically adopt a successful rollback."""
        target_status = DeploymentStatus.of(run)
        adopts_rollback = bool(
            deployment.rollback_of
            and target_status is DeploymentStatus.SUCCEEDED
            and not check
        )
        with self.persistence.uow.transaction():
            if adopts_rollback:
                rollback_policy.require_unchanged_canonical(
                    self.persistence.deployments, deployment
                )
                rollback_policy.adopt_snapshot(
                    self.persistence.zones,
                    self.persistence.sites,
                    self.dns,
                    snapshot,
                )
            deployment = self.persistence.deployments.transition(
                deployment.id,
                DeploymentStatus.RUNNING,
                target_status,
                finished_at=datetime.now(UTC),
                result=run,
            )
            if target_status is DeploymentStatus.SUCCEEDED and not check:
                self.persistence.requirements.clear(
                    DeploymentRequirementKind.CERTIFICATES
                )
            self.events.record(
                domain_event(
                    operator,
                    f"deployment.{deployment.status}",
                    "deployment",
                    deployment.id,
                    {
                        "return_code": run.return_code,
                        "changed": [host.host for host in run.changed_hosts],
                        "failed": [host.host for host in run.failed_hosts],
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
            deployment = self.persistence.deployments.transition(
                deployment.id,
                DeploymentStatus.RUNNING,
                status,
                finished_at=datetime.now(UTC),
                result=aborted_run(exc, interrupted=interrupted),
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

    def write_desired_state(self, snapshot: str, path: Path) -> None:
        """Render a snapshot as the document Ansible reads with ``--extra-vars``.

        The destination is a parameter because two callers want different ones:
        a deploy publishes to ``generated_vars_path`` under the lock, while
        ``validate`` renders to a scratch path it then throws away.
        """
        self.execution.renderer.render(snapshot, path)
