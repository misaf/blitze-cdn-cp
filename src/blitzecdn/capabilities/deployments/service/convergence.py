"""Converge a compiled release onto the fleet, and record what happened.

DeploymentService owns convergence locking, workflow records, and finalization.
Rollback policy, validation, and reporting live in their respective service
modules. Validation uses a scratch file without taking the deployment lock.

What this service does *not* do any more is decide what the fleet should serve.
It asks the releases capability to compile the current state, records the
identifier it gets back, and publishes that release's artifact — so the
document Ansible reads is the one the release says it is, and stays readable
after the run for anyone asking what a deployment actually sent.

Rollback converges an older release and restores the canonical zones, records
and rules it was compiled from, so reconciliation cannot quietly undo it. See
docs/decisions/0007-releases-and-reconciliation.md for the ownership and
transaction boundaries, and 0001 for why hosts are derived rather than stored.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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
    UnitOfWork,
    Workflows,
    YamlWriter,
    ZoneEditor,
    ZoneStore,
)
from blitzecdn.capabilities.deployments.service import reporting
from blitzecdn.capabilities.deployments.service import rollback as rollback_policy
from blitzecdn.capabilities.deployments.service.rollout import Rollout, RolloutOutcome
from blitzecdn.capabilities.deployments.service.validation import DeploymentValidation
from blitzecdn.capabilities.releases.domain import DESIRED_STATE_ARTIFACT, Release
from blitzecdn.capabilities.workflows.domain import WorkflowKind
from blitzecdn.core.domain.events import domain_event
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
    #: Per-edge rollout progress. Beside the deployment store rather than
    #: inside it because a target is written by the rollout and read by
    #: reporting, and neither is the deployment history's business.
    targets: DeploymentTargets
    zones: ZoneStore
    #: Only a rollback writes here, and only wholesale.
    rules: RuleRestore
    uow: UnitOfWork
    requirements: DeploymentRequirements


@dataclass(frozen=True)
class DeploymentExecution:
    """Collaborators that publish, launch, and observe deployment work."""

    runner: DeploymentRunner
    background: QueueBackgroundRunner
    read_log: LogReader
    #: How the release's artifact reaches disk. Atomic, because Ansible reads
    #: the file moments after this writes it and an edge converged from half a
    #: document is worse than one that did not converge at all.
    write_yaml: YamlWriter
    #: The per-edge state machine. It owns the ordering, the fence and what a
    #: failure means; this service owns the lock, the transaction and the
    #: deployment record around it.
    rollout: Rollout
    #: Which edges a run reaches, and where each answers from outside.
    addresses: EdgeAddresses


class DeploymentService:
    """Runs Ansible against a recorded release and owns the deployment lock.

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
        releases: Releases,
        workflows: Workflows,
    ) -> None:
        self.policy = policy
        self.persistence = persistence
        self.execution = execution
        self.events = events
        #: The whole of what this service knows about configuration.
        self.releases = releases
        #: Canonical DNS validation, also passed to DeploymentValidation.
        self.dns = dns
        self.workflows = workflows
        #: Built here rather than injected: every collaborator it needs is one
        #: this service was already given, and asking the composition root for
        #: a second object assembled from the same nine would put the fact that
        #: they are the same nine in a place no test of this capability can see.
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

    def validate(self, *, host_limit: str | None = None) -> list[str]:
        """Answer whether desired state is coherent and the play parses.

        The answer is ``service.validation``'s, and deliberately not reached
        under the lock this service otherwise holds
        for everything: validating is a question about the current desired
        state, and taking the lock to ask it would make ``blitzecdn validate``
        block behind a fleet convergence that has nothing to do with it.
        """
        return self._validation.errors(host_limit=host_limit)

    # -- Deploying -----------------------------------------------------

    def deploy(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment:
        """Converge the edges, returning once the run has finished.

        ``host_limit`` narrows the run to some of them — a canary. It is
        recorded on the deployment because it changes what success means: the
        release became reality on the named edges only, and the rest are
        still serving whatever they had.
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
        """Converge a prior release and adopt it as canonical desired state.

        Deliberately takes no host limit. On success this rewrites the
        canonical records, so a rollback that reached only some edges would
        leave the control plane asserting a state the rest of the fleet has
        never been given — the precise disagreement rollback exists to end.
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
        return reporting.drift_report(
            self.persistence.deployments, self.persistence.targets, deployment_id
        )

    # -- History -------------------------------------------------------

    def get_deployment(self, deployment_id: str) -> Deployment:
        """One deployment, for an operator or a client polling a queued run."""
        return self.persistence.deployments.get_deployment(deployment_id)

    def list_deployments(self, limit: int = 20) -> list[Deployment]:
        """Recent deployments, newest first."""
        return self.persistence.deployments.list_deployments(limit)

    def targets(self, deployment_id: str) -> Sequence[DeploymentTarget]:
        """How far each edge got in this deployment.

        The answer to "which edges are on the new configuration and which are
        still on the old", which a fleet-wide run could only report as an
        absence: an edge missing from a result might have succeeded silently,
        failed unreported, or never been contacted.
        """
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
    ) -> Deployment:
        """Record a QUEUED deployment. Callers must hold the deployment lock.

        ``release_id`` is supplied only by a rollback, which is converging a
        release that already exists. A forward deploy compiles the current
        state here — under the lock, so the release a deployment names is the
        state as it stood when the run was admitted rather than whatever it had
        become by the time a worker picked the row up.
        """
        # Normalised before it is stored, so the record shows what actually ran
        # rather than what was typed, and a malformed limit is refused before a
        # deployment row exists to explain.
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
                        "release_id": release.id,
                    },
                )
            )
        return deployment

    def _queue_rollback(
        self, operator: str, deployment_id: str | None, *, check: bool
    ) -> Deployment:
        target = rollback_policy.select_target(
            self.persistence.deployments, self.releases, deployment_id
        )
        return self._queue(
            operator,
            check=check,
            release_id=self.persistence.deployments.deployment_release(target.id),
            rollback_of=target.id,
            # What canonical state looks like right now. Adoption compares
            # against this and refuses if a record was written while the
            # rollback was converging — the deployment lock does not exclude
            # record writes, and restoring wholesale over one would delete it
            # with no conflict and nothing left to say it existed.
            canonical_digest=self.releases.inputs().digest,
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
        """Roll this deployment's release out edge by edge. Callers hold the lock.

        The lock is still fleet-wide — one deployment at a time — and the
        rollout inside it is per edge. Both are needed and they answer
        different questions: the lock stops two deployments overlapping, and
        the rollout's fence stops a worker that was paused past its lease from
        recording an outcome for a rollout that has since been taken over.
        """
        check = deployment.check_mode
        deployment = self.persistence.deployments.transition(
            deployment.id,
            DeploymentStatus.QUEUED,
            DeploymentStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        try:
            release = self.releases.get(deployment.release_id)
            outcome = self.execution.rollout.converge(
                deployment.id,
                release,
                edges=self.execution.addresses.selected(deployment.host_limit),
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

        The deployment's status comes from the rollout rather than from any one
        Ansible result. A run that succeeded on every task it reached is not a
        successful deployment if an edge after it was never attempted, and the
        per-edge rows are what make that difference expressible.
        """
        # A rollout that produced no Ansible result at all has one of two
        # stories, and they must not be told the same way. Either it failed
        # before running anything — the error says which edge and which phase —
        # or the fleet is empty, and there was genuinely nothing to converge.
        # The second is a success with an explanation rather than a silent one:
        # the fleet-wide run this replaced answered "skipping: no hosts
        # matched" with a zero return code and told nobody.
        run = outcome.run or aborted_run(
            RuntimeError(
                outcome.error
                or "no edges are registered, so nothing was converged; add one "
                "with 'blitzecdn edge add'"
            ),
            interrupted=False,
        )
        if outcome.succeeded:
            target_status = DeploymentStatus.SUCCEEDED
        elif outcome.run is not None:
            # The run's own reading of how it ended, so a timeout is recorded
            # as a timeout rather than flattened into a failure. `DeploymentStatus.of`
            # can still answer SUCCEEDED for a run that reported no failure —
            # a rollout stops for reasons a single run does not know about, an
            # edge that would not answer being the standing one — so a
            # successful-looking run inside a stopped rollout is a failure.
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
                    self.releases.inputs_for(release.id),
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

    def publish_artifact(self, release: Release, path: Path) -> None:
        """Put a release's desired-state artifact where Ansible will read it.

        No rendering happens here and none can: the document is already in the
        release, digested, and this writes exactly those bytes. That is the
        difference the releases capability bought — "what did that deployment
        send" is answered by the release rather than by re-deriving it from
        state that has since moved on.
        """
        self.execution.write_yaml(
            path, release.artifact(DESIRED_STATE_ARTIFACT).document
        )
