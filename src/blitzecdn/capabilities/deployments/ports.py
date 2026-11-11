from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, runtime_checkable

from blitzecdn.capabilities.deployments.domain import (
    ConvergenceHealth,
    ConvergenceKind,
    Deployment,
    DeploymentRequirementKind,
    DeploymentTarget,
    TargetPhase,
    TargetStatus,
)
from blitzecdn.capabilities.releases import Release, ReleaseInputs
from blitzecdn.capabilities.sites import Rule, ZoneEditor, ZoneStore
from blitzecdn.capabilities.workflows import Workflow, WorkflowKind
from blitzecdn.core.domain.configuration import ConfigurationBundle
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.domain.runs import AnsibleRun
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder


class RuleRestore(Protocol):
    """Restore the rules wholesale, for a rollback that adopts an older release.

    Required, not optional: rules cascade-delete with their zone, which adoption
    rewrites.
    """

    def replace_all_rules(self, rules: list[Rule]) -> None:
        """Make ``rules`` the complete rule set, removing any rule not among them."""
        ...


class SettingRestore(Protocol):
    """Restore the fleet settings wholesale, for a rollback that adopts a release."""

    def replace_all_settings(self, settings: Mapping[str, Any]) -> None:
        """Make ``settings`` the complete fleet settings, dropping any key not in it."""
        ...


class DeploymentRequirements(Protocol):
    """The durable reasons a convergence is still owed to the fleet, by typed kind."""

    def require(self, kind: DeploymentRequirementKind) -> None:
        """Record that a convergence of ``kind`` is owed, as of now.

        Requiring an already pending kind moves its request time forward, so a
        deployment compiled before this call no longer clears it.
        """
        ...

    def clear(self, kind: DeploymentRequirementKind, *, through: datetime) -> None:
        """Clear ``kind`` only if it was requested at or before ``through``."""
        ...

    def pending(self, kind: DeploymentRequirementKind) -> bool:
        """Whether a convergence of ``kind`` is still owed."""
        ...

    def requested_at(self, kind: DeploymentRequirementKind) -> datetime | None:
        """When ``kind`` was last required, or ``None`` if nothing is owed."""
        ...


class ConvergenceOutcomes(Protocol):
    """Whether each kind of convergence keeps succeeding, for alerting."""

    def record(self, kind: ConvergenceKind, *, succeeded: bool) -> None:
        """Count one finished convergence of ``kind`` towards its health.

        A success resets the run of consecutive failures; a failure extends it.
        Concurrent recorders must never lose an increment.
        """
        ...

    def health(self) -> tuple[ConvergenceHealth, ...]:
        """Every kind's record, in declaration order; an unrecorded kind reads empty."""
        ...


class DeploymentStore(Protocol):
    """Deployment history, and which release each run converged."""

    def create_deployment(
        self,
        operator: str,
        *,
        release_id: str,
        check_mode: bool,
        rollback_of: str | None = None,
        host_limit: str | None = None,
        canonical_digest: str | None = None,
    ) -> Deployment:
        """Record a new QUEUED deployment of ``release_id`` and return it.

        ``rollback_of`` names the deployment being rolled back from; ``host_limit``
        makes it a canary; ``canonical_digest`` is the state a rollback adopts.
        """
        ...

    def save(self, before: Deployment, after: Deployment) -> Deployment:
        """Record the transition ``before`` to ``after``, if nobody moved it first.

        Raises ``ConflictError`` when the stored status is no longer ``before``'s, so
        the loser of two racing processes never overwrites the winner.
        """
        ...

    def get_deployment(self, deployment_id: str) -> Deployment:
        """One deployment; raises ``NotFoundError`` if there is none by that id."""
        ...

    def deployment_release(self, deployment_id: str) -> str:
        """Which release this deployment converged, or is about to.

        Raises ``NotFoundError`` for an unknown deployment.
        """
        ...

    def paginate_deployments(
        self, *, limit: int, offset: int
    ) -> PageResult[Deployment]:
        """One page of deployment history, newest first."""
        ...

    def list_deployments(self, limit: int = 20) -> list[Deployment]:
        """The ``limit`` most recent deployments, newest first."""
        ...

    def latest_successful_applied(self) -> Deployment | None:
        """The newest successful real run, ignoring check-mode runs since."""
        ...

    def queued_deployments(self) -> list[Deployment]:
        """Deployments waiting for a worker, oldest first."""
        ...

    def running_deployments(self) -> list[Deployment]:
        """Deployments recorded as running, oldest first."""
        ...

    def prune_history(self, keep: int, *, keep_applied: int) -> int:
        """Drop finished deployments history and rollback no longer need.

        Keeps the newest ``keep`` check-mode runs, the newest ``keep_applied`` real
        runs, every rollback candidate among recent releases, and every queued or
        running row. Returns how many were removed.
        """
        ...

    def live_release(self) -> str | None:
        """The release of the newest real run that reached any edge.

        A run that succeeded, or one that failed after committing some edges, which
        stay on it. ``None`` when no real run has reached an edge.
        """
        ...

    def successful_rollback_target(self, current_release: str) -> Deployment:
        """The newest successful full-fleet run of a release not ``current_release``.

        Canaries never qualify: one edge reaching a release does not make it a state
        the fleet ran. Raises ``NotFoundError`` when there is none.
        """
        ...


class DeploymentGateway(Protocol):
    """What the certificate service needs from the deployment service.

    Whether a site is on the edges (HTTP-01 needs a served vhost), and a way to
    queue or run the convergence its material requires.
    """

    def site_is_deployed(self, site_name: str) -> bool:
        """Whether the most recent real deployment carried the site ``site_name``."""
        ...

    def deploy(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment:
        """Converge the edges under the deployment lock, returning the finished run.

        Raises ``DeploymentBusyError`` if another process holds the lock.
        """
        ...

    def converge_requirements(self, operator: str) -> Deployment | None:
        """Queue one fleet deployment for what is owed, or ``None`` if nothing is.

        A queued deployment that already satisfies every pending requirement is
        returned instead of queueing a second.
        """
        ...


class QueueBackgroundRunner(Protocol):
    """Enqueues a durable identifier for an out-of-process worker."""

    def enqueue(self, deployment_id: str) -> None:
        """Hand ``deployment_id`` to the worker; a repeat delivery is harmless."""
        ...


class DeploymentLocker(Protocol):
    """The fleet-wide deployment lock, without any way to run a playbook.

    Issuance holds it so an ACME challenge never lands mid-convergence.
    """

    def lock(self) -> AbstractContextManager[Any]:
        """The exclusive, fleet-wide deployment lock, as a context manager.

        Entering does not wait: it raises ``DeploymentBusyError`` while another
        process holds the lock.
        """
        ...


@runtime_checkable
class ContendedLock(Protocol):
    """A held deployment lock that can say whether someone is waiting for it.

    What entering ``DeploymentLocker.lock()`` gives back, when the lock can tell.
    A holder that may stop early asks between steps and lets the waiter in.
    """

    def contended(self) -> bool:
        """Whether someone was refused the lock since it was taken."""
        ...


class DeploymentRunner(DeploymentLocker, Protocol):
    """Converges the fleet, under the lock it inherits.

    Answers only with an :class:`~blitzecdn.core.domain.runs.AnsibleRun`, never raw
    output. Other capabilities' plays are declared on their own ports.
    """

    def validate(self, variables: Path) -> AnsibleRun:
        """Parse the play against ``variables``, changing nothing on any edge.

        ``variables`` is supplied rather than assumed so validation never writes over
        the desired-state file a concurrent deploy is converging.
        """
        ...

    def run(
        self,
        *,
        check: bool,
        host_limit: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> AnsibleRun:
        """Run the play against the fleet. The caller must hold :meth:`lock`.

        An empty ``tags`` runs the whole play; ``("stage",)`` stages without
        activating. ``check`` asks the edges what would change, changing nothing.
        """
        ...


class LogReader(Protocol):
    """Reads back a run log tail, for quoting to an operator, never for branching."""

    def __call__(self, path: Path | str | None, *, limit: int) -> str: ...


class YamlWriter(Protocol):
    """Publishes the desired-state document Ansible reads, atomically.

    Ansible reads it moments after a deploy writes it; a partial document must
    never be observable. Positional-only, so implementations may name parameters
    freely.
    """

    def __call__(self, path: Path, document: dict[str, object], /) -> None: ...


class Releases(Protocol):
    """Compiling desired state, and reading back what was compiled.

    All a deployment knows about configuration: it converges a release's artifact
    and derives nothing itself. ``compile`` records nothing, so repeated
    validation writes no history; ``prepare`` records.
    """

    def inputs(self) -> ReleaseInputs:
        """Canonical desired state as it stands right now."""
        ...

    def compile(self, *, host_limit: str | None = None) -> Release:
        """Compile the current state without recording anything."""
        ...

    def prepare(self, *, host_limit: str | None = None) -> Release:
        """Compile the current state and put it on record, idempotently by digest."""
        ...

    def get(self, release_id: str) -> Release:
        """One stored release; raises ``NotFoundError`` if there is none by that id."""
        ...

    def inputs_for(self, release_id: str) -> ReleaseInputs:
        """The canonical state a stored release was compiled from."""
        ...

    def configuration(self, release_id: str) -> ConfigurationBundle:
        """The configuration a prepared release rendered to, as it was rendered.

        Raises ``NotFoundError`` when none was recorded; a deployment refuses to ship
        such a release.
        """
        ...


class WorkflowProgress(Protocol):
    """The checkpoint sink a convergence writes its progress to.

    ``fail`` lets a convergence that handled its errors mark the workflow failed
    without raising.
    """

    #: Both positional and required, matching the closure the coordinator builds.
    checkpoint: Callable[[str, dict[str, Any] | None], Workflow]

    def fail(self, error: str) -> None:
        """Mark the workflow failed with ``error`` once it closes, without raising."""
        ...


class WorkflowRun(Protocol):
    def __enter__(self) -> WorkflowProgress: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class Workflows(Protocol):
    """Opening a journal entry, and recovering the ones a restart abandoned."""

    def run(
        self, kind: WorkflowKind, operator: str, resource_id: str | None = None
    ) -> WorkflowRun:
        """Open a journalled workflow, closed when the ``with`` block exits.

        Leaving the block normally records success, or failure if
        ``fail`` was called; an exception records failure and
        propagates.
        """
        ...

    def reconcile_interrupted(
        self, *, resource_id: str | None = None
    ) -> Sequence[Workflow]:
        """Mark unfinished workflows NEEDS_REVIEW, returning them.

        Every one at startup, under the deployment lock; given ``resource_id``, only
        that resource's, leaving work still running elsewhere.
        """
        ...


__all__ = [
    "ContendedLock",
    "DeploymentGateway",
    "DeploymentLocker",
    "DeploymentRequirements",
    "DeploymentRunner",
    "DeploymentStore",
    "DeploymentTargets",
    "EdgeAddresses",
    "EventRecorder",
    "LogReader",
    "QueueBackgroundRunner",
    "Releases",
    "RuleRestore",
    "ServingVerifier",
    "SettingRestore",
    "UnitOfWork",
    "WorkflowProgress",
    "WorkflowRun",
    "Workflows",
    "YamlWriter",
    "ZoneEditor",
    "ZoneStore",
]


class DeploymentTargets(Protocol):
    """Per-edge rollout progress, and the fence every write to it names."""

    def claim_generation(self, deployment_id: str) -> int:
        """Take this deployment over, returning the new fence.

        Every older fence stops matching in the same commit, so a worker that
        outlived its lease writes nothing more.
        """
        ...

    def plan(self, deployment_id: str, edges: Sequence[str], *, fence: int) -> None:
        """Record the edges this rollout aims at, stamping every row with ``fence``.

        Missing rows are added and existing progress kept, so a resumed rollout picks
        up new edges. Raises ``ConflictError`` if ``fence`` is no longer current.
        """
        ...

    def paginate_targets(
        self, deployment_id: str, *, limit: int, offset: int
    ) -> PageResult[DeploymentTarget]:
        """One page of this deployment's per-edge progress, ordered by edge."""
        ...

    def targets(self, deployment_id: str) -> Sequence[DeploymentTarget]:
        """Every edge's progress in this deployment, ordered by edge."""
        ...

    def begin(
        self, deployment_id: str, edge: str, *, fence: int, now: datetime
    ) -> bool:
        """Mark ``edge`` running and count an attempt; ``False`` if fenced out."""
        ...

    def record_phase(
        self, deployment_id: str, edge: str, phase: TargetPhase, *, fence: int
    ) -> bool:
        """Mark ``phase`` completed for ``edge``; ``False`` if fenced out."""
        ...

    def finish(
        self,
        deployment_id: str,
        edge: str,
        *,
        fence: int,
        status: TargetStatus,
        now: datetime,
        error: str | None = None,
    ) -> bool:
        """Record how ``edge`` ended, and any error; ``False`` if fenced out."""
        ...

    def skip_remaining(
        self, deployment_id: str, *, fence: int, now: datetime, reason: str
    ) -> int:
        """Mark every edge not yet attempted SKIPPED, with ``reason``.

        None of them is known bad. Returns how many were skipped; a stale ``fence``
        skips none.
        """
        ...


class EdgeAddresses(Protocol):
    """Which edges a run reaches, and where each answers from outside.

    ``selected`` uses the same matcher as the Ansible ``--limit``.
    ``public_address`` is where a visitor arrives, which differs from the SSH
    address on NAT'd or multi-homed edges.
    """

    def selected(self, host_limit: str | None) -> Sequence[str]:
        """The names of the edges ``host_limit`` selects; ``None`` selects all."""
        ...

    def public_address(self, edge: str) -> str | None:
        """Where visitors reach ``edge``, or ``None`` for an unknown edge."""
        ...


class ServingVerifier(Protocol):
    """Whether an edge actually answers for what it was told to serve."""

    def verify(
        self, *, edge: str, address: str, sites: Sequence[CdnSite], release_digest: str
    ) -> Sequence[Any]:
        """Ask ``edge`` at ``address`` for one hostname of each site it should serve.

        ``release_digest`` is the release the edge should report having activated.
        Returns one result per hostname probed; sites without hostnames are skipped.
        """
        ...
