"""Converging a fleet one edge at a time, with the progress written down.

The rollout used to be a single Ansible run with ``serial`` batching and
``any_errors_fatal`` inside the play. That is a correct policy — canary first,
stop on the first failure — implemented in a place where it could not be
resumed: the record of which edges had already converged lived in the run's
event stream, so a controller that died halfway through came back knowing only
that a deployment had been abandoned.

The policy is unchanged and has moved out here, where each edge's progress is a
row. What that buys is in
:mod:`~blitzecdn.capabilities.deployments.domain.target`; what this module owns
is the ordering, the fence, and the decision about what a failure means.

Fencing
-------
Taking a deployment over increments its generation, and every write to a target
row names the generation it was read at. A worker paused past its job lease —
the queue's lease says who *may* converge; this says whose result is *recorded*
— comes back to find its generation stale and its writes matching nothing. It
is stopped by the fence rather than by being asked to notice it has been away.

Stopping
--------
The first edge to fail stops the rollout, and the edges after it are recorded
SKIPPED rather than FAILED. That distinction is load-bearing: a skipped edge is
still serving whatever it had and nothing is known to be wrong with it, and
reporting one as a failure sends an operator to look at a host that is fine.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from blitzecdn.capabilities.deployments.domain import (
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
from blitzecdn.capabilities.releases.domain import Release
from blitzecdn.core.domain.runs import AnsibleRun
from blitzecdn.core.exceptions import ExecutionError

__all__ = ["Rollout", "RolloutOutcome"]

_LOGGER = logging.getLogger(__name__)

#: The tag selection that runs preparation and nothing that changes what an
#: edge is serving. `stage` is declared on every pre-task of the edge play and
#: on no role, and the pre-tasks also carry `always` so they still run as
#: preconditions under every other selection.
_STAGE_TAGS = ("stage",)


@dataclass(frozen=True)
class RolloutOutcome:
    """What a rollout did, per edge, and whether the fleet reached the release."""

    targets: tuple[DeploymentTarget, ...]
    #: The last Ansible result produced, kept because the deployment record
    #: still carries one and an operator still reads it. A per-edge rollout has
    #: several; this is the one that decided the outcome — the first failure,
    #: or the last success.
    run: AnsibleRun | None
    #: Why the rollout stopped, when it stopped early.
    error: str | None = None

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
        """Edges this rollout aimed at and never touched.

        The difference between "the deploy failed" and "the fleet is now
        running two configurations", which a fleet-wide run could only report
        as an absence.
        """
        return tuple(
            target.edge
            for target in self.targets
            if target.status in (TargetStatus.PENDING, TargetStatus.SKIPPED)
        )


class Rollout:
    """Walks each edge through the phases, recording every step."""

    def __init__(
        self,
        *,
        targets: DeploymentTargets,
        runner: DeploymentRunner,
        addresses: EdgeAddresses,
        verifier: ServingVerifier,
        publish: Callable[[Release], None],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.targets = targets
        self.runner = runner
        self.addresses = addresses
        self.verifier = verifier
        #: Puts the release's artifact where Ansible will read it. A callable
        #: rather than a port, because it is one bound method of the service
        #: that owns this rollout and giving it a Protocol of its own would be
        #: naming a seam that has one side.
        self.publish = publish
        self.clock = clock

    def converge(
        self,
        deployment_id: str,
        release: Release,
        *,
        edges: Sequence[str],
        check: bool,
    ) -> RolloutOutcome:
        """Take every edge through the phases, stopping at the first failure.

        ``check`` narrows the whole rollout to its ``VALIDATE`` phase: a
        check-mode run reports what would change on each edge and changes
        nothing, so staging, activating and verifying are all things it must
        not do. That is what makes ``blitzecdn plan`` and the hourly drift
        check safe to run against a live fleet.
        """
        fence = self.targets.claim_generation(deployment_id)
        self.targets.plan(deployment_id, edges, fence=fence)
        phases = self._phases(check)
        # The artifact is published once, here, and not once per edge: it is a
        # fact about the deployment rather than about any host, and every edge
        # in a rollout converges the same bytes. Doing it before the loop is
        # also what keeps a fleet with no edges registered behaving as it
        # always did — the desired state for this release still reaches disk,
        # and an operator can look at what would have been sent.
        self.publish(release)

        last_run: AnsibleRun | None = None
        for target in self.targets.targets(deployment_id):
            if target.status is TargetStatus.SUCCEEDED:
                # Resuming an interrupted rollout: this edge is already there.
                continue
            outcome, last_run = self._converge_edge(
                deployment_id, target, release, phases, fence=fence, check=check
            )
            if outcome is not None:
                self.targets.skip_remaining(
                    deployment_id,
                    fence=fence,
                    now=self.clock(),
                    reason=(
                        f"the rollout stopped at {target.edge}: {outcome}. "
                        "This edge was not attempted and is serving what it had."
                    ),
                )
                return RolloutOutcome(
                    targets=tuple(self.targets.targets(deployment_id)),
                    run=last_run,
                    error=f"{target.edge}: {outcome}",
                )
        return RolloutOutcome(
            targets=tuple(self.targets.targets(deployment_id)), run=last_run
        )

    @staticmethod
    def _phases(check: bool) -> tuple[TargetPhase, ...]:
        if check:
            return (TargetPhase.PREPARE, TargetPhase.VALIDATE)
        return (
            TargetPhase.PREPARE,
            TargetPhase.VALIDATE,
            TargetPhase.STAGE,
            TargetPhase.ACTIVATE,
            TargetPhase.VERIFY,
        )

    def _converge_edge(
        self,
        deployment_id: str,
        target: DeploymentTarget,
        release: Release,
        phases: tuple[TargetPhase, ...],
        *,
        fence: int,
        check: bool,
    ) -> tuple[str | None, AnsibleRun | None]:
        """One edge through every phase. Returns the failure, or ``None``.

        Every phase is idempotent, so a resumed edge re-runs the phase it was
        interrupted in rather than this having to reason about how far into it
        a dead process had got. Re-publishing an artifact, re-checking a play
        and re-converging an already-converged edge all cost time and change
        nothing.
        """
        if not self.targets.begin(
            deployment_id, target.edge, fence=fence, now=self.clock()
        ):
            # The fence moved: another worker owns this rollout now. Reporting
            # a failure here would be this worker's opinion about an edge it no
            # longer has any claim on.
            return "the rollout was taken over by another worker", None

        last_run: AnsibleRun | None = None
        for phase in phases:
            if target.phase is not None and _already_done(target.phase, phase):
                continue
            try:
                produced = self._run_phase(phase, target.edge, release, check=check)
            except ExecutionError as exc:
                # Ansible could not be executed at all: a missing binary, an
                # unreadable inventory, a controller that is misconfigured. That
                # is not this edge's failure and no other edge would fare
                # better, so the row is closed out honestly and the error goes
                # up to the caller, which fails the deployment and re-raises.
                # Swallowing it here would report a fleet-wide breakage as one
                # edge declining to converge.
                self._fail(
                    deployment_id,
                    target.edge,
                    fence,
                    phase,
                    f"{type(exc).__name__}: {exc}",
                )
                raise
            except Exception as exc:
                _LOGGER.exception(
                    "deployment %s failed on %s during %s",
                    deployment_id,
                    target.edge,
                    phase.value,
                )
                return self._fail(
                    deployment_id,
                    target.edge,
                    fence,
                    phase,
                    f"{type(exc).__name__}: {exc}",
                ), last_run
            # Kept only when the phase produced one. `PREPARE` and `VERIFY`
            # answer `None` — one writes nothing and the other makes an HTTP
            # request — and letting either clear this would leave a successful
            # deployment carrying no Ansible result at all, because verifying
            # is the last thing a rollout does.
            last_run = produced or last_run
            if produced is not None and not produced.succeeded:
                return self._fail(
                    deployment_id,
                    target.edge,
                    fence,
                    phase,
                    produced.summary() or "the run reported a failure",
                ), last_run
            if not self.targets.record_phase(
                deployment_id, target.edge, phase, fence=fence
            ):
                return "the rollout was taken over by another worker", last_run

        self.targets.finish(
            deployment_id,
            target.edge,
            fence=fence,
            status=TargetStatus.SUCCEEDED,
            now=self.clock(),
        )
        return None, last_run

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

    def _run_phase(
        self, phase: TargetPhase, edge: str, release: Release, *, check: bool
    ) -> AnsibleRun | None:
        """Do one phase for one edge, or nothing when the phase is not a run.

        ``PREPARE`` and ``VERIFY`` produce no Ansible result — one writes a
        file on the controller and the other makes an HTTP request — so both
        answer ``None`` and report failure by raising. That keeps the caller's
        two failure paths honest: a run that reported a failure, and a phase
        that could not be carried out at all.
        """
        if phase is TargetPhase.PREPARE:
            # Nothing to do per edge: `converge` published the artifact once,
            # before the loop. The phase is still recorded on every target
            # because a target's row is meant to tell that edge's whole story
            # without joining anything — "this edge got as far as prepare"
            # reads correctly whether or not the work was shared.
            return None
        if phase is TargetPhase.VALIDATE:
            return self.runner.run(check=True, host_limit=edge)
        if phase is TargetPhase.STAGE:
            return self.runner.run(check=False, host_limit=edge, tags=_STAGE_TAGS)
        if phase is TargetPhase.ACTIVATE:
            return self.runner.run(check=check, host_limit=edge)
        self._verify(edge, release)
        return None

    def _verify(self, edge: str, release: Release) -> None:
        """Require the edge to answer for what the release says it serves.

        Raises rather than returning a result, because from the rollout's point
        of view an edge that does not answer is indistinguishable from a phase
        that could not run: both mean this edge did not reach the release.

        An edge with no public address is verified vacuously and says so in the
        log. That is not a silent pass — the address is where a probe would
        connect, and an edge that has not declared one is an edge nobody has
        told us how to reach from outside. Failing the rollout for it would
        make declaring a public address mandatory, which is a product decision
        this phase has no business making on its own.
        """
        address = self.addresses.public_address(edge)
        if address is None:
            _LOGGER.info(
                "edge %s declares no public address; skipping serving verification",
                edge,
            )
            return
        sites = tuple(entry.site for entry in release.sites)
        results = self.verifier.verify(edge=edge, address=address, sites=sites)
        unserved = [result for result in results if not result.served]
        if unserved:
            detail = "; ".join(
                f"{result.hostname}: {result.detail}" for result in unserved
            )
            raise RuntimeError(
                f"{edge} accepted the configuration but is not serving "
                f"{len(unserved)} of {len(results)} hostname(s): {detail}"
            )


def _already_done(reached: TargetPhase, phase: TargetPhase) -> bool:
    """Whether a resumed edge has already completed this phase.

    Compared by position rather than by equality, because "already done" means
    "at or before the phase this edge last completed" and an enum has no order
    of its own worth relying on.
    """
    from blitzecdn.capabilities.deployments.domain import PHASE_ORDER

    return PHASE_ORDER.index(phase) <= PHASE_ORDER.index(reached)
