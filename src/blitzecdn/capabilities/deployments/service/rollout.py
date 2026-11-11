"""Converging a fleet canary first, then in bounded batches, with progress written down.

The first edge goes alone and must succeed before any other is touched. After
that, edges move in batches of at most ``batch_size``: one Ansible run per phase
per batch, each run limited to that batch, read back per host from the event
stream. A failure stops the rollout once the batch it happened in is settled.
Pacing is done here rather than by ``serial`` in the play, so progress is a row
and a rollout can resume. Each takeover bumps a generation that every target
write names: the queue's lease says who may converge, the fence says whose result
is recorded. Edges after a failure are SKIPPED, not FAILED — they are still
serving what they had.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from blitzecdn.capabilities.deployments.domain import (
    PHASE_ORDER,
    DeploymentTarget,
    TargetPhase,
    TargetStatus,
)
from blitzecdn.capabilities.deployments.ports import (
    DeploymentRunner,
    DeploymentTargets,
    EdgeAddresses,
    ServingVerifier,
)
from blitzecdn.capabilities.releases import Release
from blitzecdn.core.domain.runs import AnsibleRun, HostRun
from blitzecdn.core.domain.validation import MAX_EDGE_LIMIT_LENGTH
from blitzecdn.core.exceptions import ExecutionError
from blitzecdn.core.ports.clock import Clock

__all__ = ["DEFAULT_BATCH_SIZE", "Rollout", "RolloutOutcome"]

_LOGGER = logging.getLogger(__name__)

#: Edges converged together after the canary, unless configured otherwise.
DEFAULT_BATCH_SIZE = 10

#: Render and validate a sealed candidate without changing the active pointer.
_STAGE_TAGS = ("stage",)

#: Phases after which an edge's serving state has changed, so a failure there is
#: withdrawn rather than merely recorded.
_SERVING_PHASES = (TargetPhase.ACTIVATE, TargetPhase.VERIFY)

_TAKEN_OVER = "the rollout was taken over by another worker"


@dataclass(frozen=True)
class RolloutOutcome:
    """What a rollout did, per edge, and whether the fleet reached the release."""

    targets: tuple[DeploymentTarget, ...]
    #: The deciding Ansible result — first failure, or last success — which the
    #: deployment record carries. A check-mode rollout's runs are merged, so the
    #: record holds every edge it validated, not only the last batch.
    run: AnsibleRun | None
    #: Why the rollout stopped, when it stopped early.
    error: str | None = None
    #: Stopped early to let a deployment waiting on the lock go first.
    yielded: bool = False

    @property
    def succeeded(self) -> bool:
        return self.error is None and all(
            target.status is TargetStatus.SUCCEEDED for target in self.targets
        )

    @property
    def converged(self) -> tuple[str, ...]:
        return tuple(
            target.edge
            for target in self.targets
            if target.status is TargetStatus.SUCCEEDED
        )

    @property
    def unattempted(self) -> tuple[str, ...]:
        """Edges this rollout aimed at and never touched."""
        return tuple(
            target.edge
            for target in self.targets
            if target.status in (TargetStatus.PENDING, TargetStatus.SKIPPED)
        )


@dataclass(frozen=True)
class _Miss:
    """Why one edge in a batch did not pass a phase."""

    detail: str
    #: The edge did not fail itself: the play stopped it because another edge in
    #: the same run failed (``any_errors_fatal``).
    collateral: bool = False


@dataclass(frozen=True)
class _Stop:
    """The edge that stopped a rollout, and what it said."""

    edge: str
    message: str


class Rollout:
    """Walks the fleet through the phases, canary first, recording every step."""

    def __init__(
        self,
        *,
        targets: DeploymentTargets,
        runner: DeploymentRunner,
        addresses: EdgeAddresses,
        verifier: ServingVerifier,
        publish: Callable[[Release], None],
        clock: Clock,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if batch_size < 1:
            raise ValueError("a rollout batch holds at least one edge")
        self.targets = targets
        self.runner = runner
        self.addresses = addresses
        self.verifier = verifier
        #: A bound method of the owning service; a Protocol would name a one-sided seam.
        self.publish = publish
        self.clock = clock
        self.batch_size = batch_size

    def converge(
        self,
        deployment_id: str,
        release: Release,
        *,
        edges: Sequence[str],
        check: bool,
        should_yield: Callable[[], bool] | None = None,
    ) -> RolloutOutcome:
        """Take every edge through the phases, stopping after the first failure.

        ``check`` limits the rollout to ``VALIDATE``, which is what makes ``plan`` and
        the drift check safe on a live fleet; it changes nothing, so it needs no
        canary. ``should_yield`` is asked before each batch: when it answers true the
        rollout stops there, leaving the rest unattempted, so whoever is waiting for
        the deployment lock gets it.
        """
        fence = self.targets.claim_generation(deployment_id)
        self.targets.plan(deployment_id, edges, fence=fence)
        phases = self._phases(check)
        # Published once per deployment, before any batch, so even an empty fleet
        # has the release's desired state on disk to inspect.
        self.publish(release)

        runs: list[AnsibleRun] = []
        planned = tuple(self.targets.targets(deployment_id))
        if not check:
            stop = self._reobserve(deployment_id, planned, release, fence=fence)
            if stop is not None:
                return self._stopped(
                    deployment_id, stop, runs, fence=fence, check=check
                )

        pending = [t for t in planned if t.status is not TargetStatus.SUCCEEDED]
        # A resumed rollout whose canary already passed goes straight to batches.
        canary = not check and not any(
            t.status is TargetStatus.SUCCEEDED for t in planned
        )
        checked = 0
        for batch in self._batches(pending, canary=canary):
            if should_yield is not None and should_yield():
                return self._yielded(deployment_id, runs, checked, fence=fence)
            stop = self._converge_batch(
                deployment_id, batch, release, phases, runs, fence=fence, check=check
            )
            if stop is not None:
                return self._stopped(
                    deployment_id, stop, runs, fence=fence, check=check
                )
            checked += len(batch)
        return RolloutOutcome(
            targets=tuple(self.targets.targets(deployment_id)),
            run=_deciding(runs, check=check),
        )

    @staticmethod
    def _phases(check: bool) -> tuple[TargetPhase, ...]:
        if check:
            return (TargetPhase.PREPARE, TargetPhase.VALIDATE)
        return PHASE_ORDER

    def _batches(
        self, pending: Sequence[DeploymentTarget], *, canary: bool
    ) -> Iterator[list[DeploymentTarget]]:
        """The canary alone, then groups of ``batch_size`` a host limit can name.

        A batch is also cut short where its names would no longer fit in one host
        limit, which is what each run is given.
        """
        remaining = list(pending)
        if canary and remaining:
            yield remaining[:1]
            remaining = remaining[1:]
        batch: list[DeploymentTarget] = []
        length = 0
        for target in remaining:
            extra = len(target.edge) + (1 if batch else 0)
            if batch and (
                len(batch) >= self.batch_size or length + extra > MAX_EDGE_LIMIT_LENGTH
            ):
                yield batch
                batch, length, extra = [], 0, len(target.edge)
            batch.append(target)
            length += extra
        if batch:
            yield batch

    def _reobserve(
        self,
        deployment_id: str,
        planned: Sequence[DeploymentTarget],
        release: Release,
        *,
        fence: int,
    ) -> _Stop | None:
        """Re-verify edges a resumed rollout already converged, before trusting them."""
        for target in planned:
            if target.status is not TargetStatus.SUCCEEDED:
                continue
            try:
                self._verify(target.edge, release)
            except Exception as exc:
                _LOGGER.exception("recovery verification failed on %s", target.edge)
                return _Stop(
                    target.edge,
                    self._fail(
                        deployment_id,
                        target.edge,
                        fence,
                        TargetPhase.VERIFY,
                        f"recovery observation failed: {exc}",
                    ),
                )
        return None

    def _converge_batch(
        self,
        deployment_id: str,
        batch: Sequence[DeploymentTarget],
        release: Release,
        phases: tuple[TargetPhase, ...],
        runs: list[AnsibleRun],
        *,
        fence: int,
        check: bool,
    ) -> _Stop | None:
        """One batch through every phase; returns what stopped it, or ``None``.

        Every phase is idempotent, so a resumed edge simply re-runs its interrupted
        one. A failure before activation stops the whole batch there; one during
        activation or verification lets the edges that passed finish, since their
        serving state has already changed.
        """
        live: dict[str, DeploymentTarget] = {}
        for target in batch:
            if not self.targets.begin(
                deployment_id, target.edge, fence=fence, now=self.clock()
            ):
                # The fence moved: another worker owns this rollout now.
                return _Stop(target.edge, _TAKEN_OVER)
            live[target.edge] = target

        stop: _Stop | None = None
        for phase in phases:
            due = [
                edge
                for edge, target in live.items()
                if target.phase is None or not _already_done(target.phase, phase)
            ]
            if not due:
                continue
            misses = self._attempt(
                deployment_id, phase, due, release, runs, fence=fence, check=check
            )
            lost = self._record(deployment_id, phase, due, misses, fence=fence)
            if lost is not None:
                return lost
            if not misses:
                continue
            for edge in misses:
                live.pop(edge)
            stop = stop or self._settle(
                deployment_id, phase, misses, fence=fence, check=check
            )
            if phase not in _SERVING_PHASES:
                # Nothing in this batch is serving the release yet; keep it so.
                for edge in live:
                    self._skip(
                        deployment_id,
                        edge,
                        fence,
                        phase,
                        f"another edge in its batch failed: {stop.message}",
                    )
                return stop

        for edge in live:
            self.targets.finish(
                deployment_id,
                edge,
                fence=fence,
                status=TargetStatus.SUCCEEDED,
                now=self.clock(),
            )
        return stop

    def _attempt(
        self,
        deployment_id: str,
        phase: TargetPhase,
        due: Sequence[str],
        release: Release,
        runs: list[AnsibleRun],
        *,
        fence: int,
        check: bool,
    ) -> dict[str, _Miss]:
        """Run one phase, closing every row first if Ansible cannot run at all."""
        try:
            return self._run_phase(phase, due, release, runs, check=check)
        except ExecutionError as exc:
            # A controller fault, not these edges'. Close their rows and re-raise
            # to fail the deployment.
            for edge in due:
                detail = self._withdraw(phase, edge, str(exc), check=check)
                self._fail(deployment_id, edge, fence, phase, detail)
            raise

    def _record(
        self,
        deployment_id: str,
        phase: TargetPhase,
        due: Sequence[str],
        misses: dict[str, _Miss],
        *,
        fence: int,
    ) -> _Stop | None:
        """Mark ``phase`` done for every edge that passed; stop if fenced out."""
        for edge in due:
            if edge in misses:
                continue
            if not self.targets.record_phase(deployment_id, edge, phase, fence=fence):
                return _Stop(edge, _TAKEN_OVER)
        return None

    def _settle(
        self,
        deployment_id: str,
        phase: TargetPhase,
        misses: dict[str, _Miss],
        *,
        fence: int,
        check: bool,
    ) -> _Stop:
        """Close every edge that missed ``phase``; return the one to blame.

        An edge the play only stopped is SKIPPED before activation, since it was
        never changed; from activation on it is withdrawn and FAILED like the rest.
        """
        messages: dict[str, str] = {}
        for edge, miss in misses.items():
            if miss.collateral and phase not in _SERVING_PHASES:
                self._skip(deployment_id, edge, fence, phase, miss.detail)
                continue
            messages[edge] = self._fail(
                deployment_id,
                edge,
                fence,
                phase,
                self._withdraw(phase, edge, miss.detail, check=check),
            )
        # Name an edge that failed itself, not one the play stopped with it.
        culprit = next(
            (edge for edge, miss in misses.items() if not miss.collateral),
            next(iter(misses)),
        )
        return _Stop(
            culprit,
            messages.get(culprit) or f"{phase.value} failed: {misses[culprit].detail}",
        )

    def _run_phase(
        self,
        phase: TargetPhase,
        edges: Sequence[str],
        release: Release,
        runs: list[AnsibleRun],
        *,
        check: bool,
    ) -> dict[str, _Miss]:
        """Do one phase for a batch; returns why each edge that did not pass missed.

        VERIFY observes each edge first, then commits the activation of those that
        answered with a tool run. Observation failures are that edge's alone.
        """
        if phase is TargetPhase.PREPARE:
            # Shared work done once in `converge`, still recorded on every target.
            return {}
        if phase is TargetPhase.VERIFY:
            misses: dict[str, _Miss] = {}
            for edge in edges:
                try:
                    self._verify(edge, release)
                except Exception as exc:
                    _LOGGER.exception("verification failed on %s", edge)
                    misses[edge] = _Miss(f"{type(exc).__name__}: {exc}")
            served = [edge for edge in edges if edge not in misses]
            if served:
                # Only an external observation may commit the edge-local journal.
                # Replaying this after a lost reply is safe: commit is idempotent.
                misses.update(
                    self._judged(
                        self._run(runs, served, check=False, tags=("commit",)),
                        served,
                    )
                )
            return misses
        if phase is TargetPhase.VALIDATE:
            produced = self._run(runs, edges, check=True)
        elif phase is TargetPhase.STAGE:
            produced = self._run(runs, edges, check=False, tags=_STAGE_TAGS)
        else:
            produced = self._run(runs, edges, check=check, tags=("activate",))
        return self._judged(produced, edges)

    def _run(
        self,
        runs: list[AnsibleRun],
        edges: Sequence[str],
        *,
        check: bool,
        tags: tuple[str, ...] = (),
    ) -> AnsibleRun:
        produced = self.runner.run(check=check, host_limit=",".join(edges), tags=tags)
        runs.append(produced)
        return produced

    @staticmethod
    def _judged(run: AnsibleRun, edges: Sequence[str]) -> dict[str, _Miss]:
        """Read one run per edge: which failed, and which the play stopped instead.

        A clean exit is a pass for every edge in it. Otherwise an edge that reported
        a failure failed; the rest stopped with it, because the play is
        ``any_errors_fatal``. A run with no failing edge of its own (a timeout, a
        crash) fails every edge it was given.
        """
        if run.succeeded:
            return {}
        fallback = run.summary() or "the run reported a failure"
        if len(edges) == 1:
            return {edges[0]: _Miss(fallback)}
        own = {
            edge: _Miss(_host_summary(host))
            for edge in edges
            if (host := run.host(edge)) is not None and not host.succeeded
        }
        if not own:
            return {edge: _Miss(fallback) for edge in edges}
        culprits = ", ".join(own)
        return {
            edge: own.get(edge)
            or _Miss(f"stopped because {culprits} failed in the same run", True)
            for edge in edges
        }

    def _fail(
        self,
        deployment_id: str,
        edge: str,
        fence: int,
        phase: TargetPhase,
        detail: str,
    ) -> str:
        message = f"{phase.value} failed: {detail}"
        self.targets.finish(
            deployment_id,
            edge,
            fence=fence,
            status=TargetStatus.FAILED,
            now=self.clock(),
            error=message,
        )
        return message

    def _skip(
        self,
        deployment_id: str,
        edge: str,
        fence: int,
        phase: TargetPhase,
        detail: str,
    ) -> None:
        """Close an edge that stopped before its serving state changed."""
        self.targets.finish(
            deployment_id,
            edge,
            fence=fence,
            status=TargetStatus.SKIPPED,
            now=self.clock(),
            error=(
                f"stopped at {phase.value}: {detail}. This edge is serving what it had."
            ),
        )

    def _stopped(
        self,
        deployment_id: str,
        stop: _Stop,
        runs: Sequence[AnsibleRun],
        *,
        fence: int,
        check: bool,
    ) -> RolloutOutcome:
        self.targets.skip_remaining(
            deployment_id,
            fence=fence,
            now=self.clock(),
            reason=(
                f"the rollout stopped at {stop.edge}: {stop.message}. "
                "This edge was not attempted and is serving what it had."
            ),
        )
        return RolloutOutcome(
            targets=tuple(self.targets.targets(deployment_id)),
            run=_deciding(runs, check=check),
            error=f"{stop.edge}: {stop.message}",
        )

    def _yielded(
        self,
        deployment_id: str,
        runs: Sequence[AnsibleRun],
        checked: int,
        *,
        fence: int,
    ) -> RolloutOutcome:
        reason = (
            f"stood aside for a deployment waiting on the lock after {checked} edge(s)"
        )
        self.targets.skip_remaining(
            deployment_id,
            fence=fence,
            now=self.clock(),
            reason=f"the check {reason}. This edge was not checked.",
        )
        return RolloutOutcome(
            targets=tuple(self.targets.targets(deployment_id)),
            run=_deciding(runs, check=True),
            error=f"the check {reason}",
            yielded=True,
        )

    def _withdraw(
        self, phase: TargetPhase, edge: str, detail: str, *, check: bool
    ) -> str:
        """Withdraw an uncommitted activation, keeping both failure reports.

        The edge's journal holds the previous state, so this works across a worker
        restart. A failed withdrawal is never reported as a restored service.
        """
        if check or phase not in _SERVING_PHASES:
            return detail
        try:
            result = self.runner.run(check=False, host_limit=edge, tags=("rollback",))
            if not result.succeeded:
                return f"{detail}; rollback failed: {result.summary()}"
        except Exception as exc:
            _LOGGER.exception("configuration rollback failed on %s", edge)
            return f"{detail}; rollback failed: {exc}"
        return (
            f"{detail}; previous configuration restored "
            "(or failed first installation withdrawn)"
        )

    def _verify(self, edge: str, release: Release) -> None:
        """Require the edge to answer for what the release says it serves; raise if not.

        An edge with no verification address fails rather than passing silently.
        """
        address = self.addresses.public_address(edge)
        if address is None:
            raise RuntimeError(f"edge {edge} has no address for release verification")
        sites = tuple(entry.site for entry in release.sites)
        results = self.verifier.verify(
            edge=edge, address=address, sites=sites, release_digest=release.digest
        )
        unserved = [result for result in results if not result.served]
        if unserved:
            detail = "; ".join(
                f"{result.hostname}: {result.detail}" for result in unserved
            )
            raise RuntimeError(
                f"{edge} accepted the configuration but is not serving "
                f"{len(unserved)} of {len(results)} hostname(s): {detail}"
            )


def _host_summary(host: HostRun) -> str:
    """One host's first failure, in the shape ``AnsibleRun.summary`` uses."""
    for failure in host.failures:
        detail = (failure.message or failure.outcome.value).strip()
        return f"{host.host}: {failure.task}: {detail}"
    if not host.reached:
        return f"{host.host}: unreachable"
    return f"{host.host}: the run reported a failure"


def _deciding(runs: Sequence[AnsibleRun], *, check: bool) -> AnsibleRun | None:
    """The result a deployment record carries.

    A real rollout's last run decides: it is the failure that stopped it, or the
    last success. A check-mode rollout's runs are each one batch's validation, so
    they are merged into one result naming every edge, the first failure deciding.
    """
    if not runs:
        return None
    if not check or len(runs) == 1:
        return runs[-1]
    deciding = next((run for run in runs if not run.succeeded), runs[-1])
    return deciding.model_copy(
        update={
            "hosts": tuple(host for run in runs for host in run.hosts),
            "targeted": tuple(
                dict.fromkeys(edge for run in runs for edge in run.targeted)
            ),
            "started_at": runs[0].started_at,
            "finished_at": runs[-1].finished_at,
        }
    )


def _already_done(reached: TargetPhase, phase: TargetPhase) -> bool:
    """Whether a resumed edge has already completed this phase, by phase order."""
    return PHASE_ORDER.index(phase) <= PHASE_ORDER.index(reached)
