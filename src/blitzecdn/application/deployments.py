"""Convergence, history, rollback, and drift.

Everything here is about turning a stored snapshot into a run of Ansible and
recording what happened. Nothing here decides *what* should be deployed; that
is the zone editor's job, and this service reads its output through
``DnsService.sync_sites`` only when a rollback rewrites canonical state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from blitzecdn.application.ports.deployments import (
    DeploymentRequirements,
    DeploymentRunner,
    DeploymentStore,
    DesiredStateRenderer,
    EventRecorder,
    LogReader,
    QueueBackgroundRunner,
    UnitOfWork,
    ZoneEditor,
    ZoneStore,
)
from blitzecdn.application.workflows import WorkflowCoordinator
from blitzecdn.domain.deployments import (
    Deployment,
    DeploymentRequirementKind,
    DeploymentStatus,
    DriftReport,
)
from blitzecdn.domain.events import domain_event
from blitzecdn.domain.operations import WorkflowKind
from blitzecdn.domain.runs import AnsibleRun, RunStatus
from blitzecdn.domain.snapshots import (
    decode_snapshot,
    decode_snapshot_zones,
    snapshot_digest,
)
from blitzecdn.domain.validation import validate_edge_limit
from blitzecdn.exceptions import ConflictError, DeploymentBusyError, ExecutionError

_LOGGER = logging.getLogger(__name__)
_DEPLOYMENT_LOOKBACK = 50


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
    uow: UnitOfWork
    requirements: DeploymentRequirements


@dataclass(frozen=True)
class DeploymentExecution:
    """Collaborators that render, launch, and observe deployment work."""

    runner: DeploymentRunner
    background: QueueBackgroundRunner
    read_log: LogReader
    renderer: DesiredStateRenderer


class DeploymentService:
    """Runs Ansible against a recorded snapshot and owns the deployment lock."""

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
        #: Only ``sync_sites`` is ever called, and only on the rollback path.
        self.dns = dns
        self.workflows = workflows

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

        Renders to a scratch file rather than to ``generated_vars_path``.
        Validation is a question, not a publication, and it takes no lock — so
        writing to the real file would let it land between the moment a deploy
        in another process wrote its snapshot there and the moment Ansible read
        it. A rollback is where that hurts most: the fleet would converge to
        current state while the rollback still rewrote canonical records to the
        old snapshot's zones, leaving the control plane and the edges disagreeing
        in exactly the way rollback exists to end.
        """
        errors = self.policy.runtime_errors()
        errors.extend(self.dns.validation_errors())
        if not errors:
            with self._scratch_desired_state(
                self.persistence.deployments.snapshot()
            ) as variables:
                run = self.execution.runner.validate(variables)
            if not run.succeeded:
                # The one place a log is read back. `--syntax-check` executes no
                # play, so there is no structured result to explain a refusal —
                # the return code decides, and Ansible's own message is quoted
                # so the operator does not have to go and find it.
                errors.append(
                    self.execution.read_log(
                        run.log_path, limit=self.policy.output_limit_bytes
                    )
                    or run.summary()
                )
        return errors

    @contextmanager
    def _scratch_desired_state(self, snapshot: str) -> Iterator[Path]:
        """Render a snapshot somewhere only this call can see, then drop it."""
        directory = self.policy.run_dir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"validate-{uuid4().hex}.yml"
        try:
            self.write_desired_state(snapshot, path)
            yield path
        finally:
            path.unlink(missing_ok=True)

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
        """Read a recorded check-mode run as a drift report.

        Derived from the stored deployment rather than only from a live run, so
        the CLI and the API share one interpretation and an operator can revisit
        the answer a scheduled check produced without re-running it. The stored
        result is the structured one, so this reads the same object a live run
        produced rather than re-deriving anything.
        """
        deployment = self.persistence.deployments.get_deployment(deployment_id)
        if not deployment.check_mode:
            raise ConflictError(
                f"deployment {deployment.id} applied changes rather than "
                "previewing them, so its result describes what it did, not "
                "what had drifted. Run 'blitzecdn drift' instead."
            )
        return DriftReport.of(deployment)

    # -- History -------------------------------------------------------

    def get_deployment(self, deployment_id: str) -> Deployment:
        """One deployment, for an operator or a client polling a queued run."""
        return self.persistence.deployments.get_deployment(deployment_id)

    def list_deployments(self, limit: int = 20) -> list[Deployment]:
        """Recent deployments, newest first."""
        return self.persistence.deployments.list_deployments(limit)

    def site_is_deployed(self, site_name: str) -> bool:
        """Whether the most recent real deployment carried this site.

        Check-mode runs are skipped: they proved the play parses, not that any
        edge is serving the vhost. A run narrowed by ``host_limit`` counts,
        because it did install the site somewhere — which makes this check
        necessary rather than sufficient, the same caveat rollback carries about
        canaries. Only the newest successful run is consulted; an older one
        listing the site says nothing about whether it is still deployed.
        """
        for deployment in self.persistence.deployments.list_deployments(
            limit=_DEPLOYMENT_LOOKBACK
        ):
            if deployment.status is not DeploymentStatus.SUCCEEDED:
                continue
            if deployment.check_mode:
                continue
            snapshot = self.persistence.deployments.deployment_snapshot(deployment.id)
            return any(site.name == site_name for site in decode_snapshot(snapshot))
        return False

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
        target = (
            self.persistence.deployments.get_deployment(deployment_id)
            if deployment_id
            else self.persistence.deployments.successful_rollback_target(
                self.persistence.deployments.snapshot()
            )
        )
        if target.check_mode or target.status is not DeploymentStatus.SUCCEEDED:
            raise ConflictError(
                "rollback target must be a successful applied deployment"
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
                        result=self._aborted_run(exc, interrupted=False),
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

    def _require_unchanged_canonical(self, deployment: Deployment) -> None:
        """Refuse to restore wholesale over a change made while we converged.

        ``replace_all_records`` deletes every zone and record and writes the
        snapshot's back, so anything created since the rollback was queued is
        gone — and record writes deliberately do not take the deployment lock,
        which means an ordinary ``blitzecdn record create`` during a
        minutes-long fleet rollback is enough. Nothing conflicted, nothing
        failed, and the audit trail showed the record being created and never
        being removed.

        Read inside the adoption transaction, which is ``BEGIN IMMEDIATE``, so
        no writer can slip between this comparison and the restore. Raising
        here aborts that transaction and the run finalises as FAILED: the edges
        are converged to the old snapshot but canonical state is untouched, so
        an operator can retry the rollback deliberately once they have seen
        what changed.
        """
        if deployment.canonical_digest is None:
            return
        current = snapshot_digest(self.persistence.deployments.snapshot())
        if current != deployment.canonical_digest:
            raise ConflictError(
                "desired state changed while this rollback was converging, so "
                "adopting the older snapshot would delete whatever was written. "
                "The edges were converged to it; canonical records were left "
                "alone. Review the change and roll back again if it should go."
            )

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
                self._require_unchanged_canonical(deployment)
                domains, records = decode_snapshot_zones(snapshot)
                self.persistence.zones.replace_all_records(domains, records)
                self.dns.sync_sites()
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
                result=self._aborted_run(exc, interrupted=interrupted),
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

    @staticmethod
    def _aborted_run(exc: BaseException, *, interrupted: bool) -> AnsibleRun:
        """A result for a deployment that never got a run of its own.

        The runner raised before Ansible reported anything — a misconfiguration
        it refused to start with, or the process being interrupted. There are no
        hosts to describe, so this records why in the same shape every other
        outcome uses rather than leaving ``result`` empty and making every
        reader handle a second way of saying "it failed".
        """
        now = datetime.now(UTC)
        return AnsibleRun(
            id=uuid4().hex,
            playbook="",
            status=RunStatus.UNSTARTED,
            return_code=None,
            started_at=now,
            finished_at=now,
            error=(
                f"deployment interrupted: {type(exc).__name__}"
                if interrupted
                else f"deployment runner error: {type(exc).__name__}: {exc}"
            ),
        )

    def write_desired_state(self, snapshot: str, path: Path) -> None:
        """Render a snapshot as the document Ansible reads with ``--extra-vars``.

        The destination is a parameter because two callers want different ones:
        a deploy publishes to ``generated_vars_path`` under the lock, while
        ``validate`` renders to a scratch path it then throws away.
        """
        self.execution.renderer.render(snapshot, path)
