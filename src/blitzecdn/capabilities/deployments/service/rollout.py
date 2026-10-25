"""Converging a fleet one edge at a time, with the progress written down.

Canary first and stop at the first failure, done here rather than by ``serial``
in the play, so progress is a row and a rollout can resume. Each takeover bumps
a generation that every target write names: the queue's lease says who may
converge, the fence says whose result is recorded. Edges after a failure are
SKIPPED, not FAILED — they are still serving what they had.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

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
from blitzecdn.core.ports.clock import Clock, system_clock

__all__ = ["Rollout", "RolloutOutcome"]

_LOGGER = logging.getLogger(__name__)

#: Render and validate a sealed candidate without changing the active pointer.
_STAGE_TAGS = ("stage",)


@dataclass(frozen=True)
class RolloutOutcome:
    """What a rollout did, per edge, and whether the fleet reached the release."""

    targets: tuple[DeploymentTarget, ...]
    #: The deciding Ansible result — first failure, or last success — which the
    #: deployment record carries.
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
        """Edges this rollout aimed at and never touched."""
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
        clock: Clock = system_clock,
    ) -> None:
        self.targets = targets
        self.runner = runner
        self.addresses = addresses
        self.verifier = verifier
        #: A bound method of the owning service; a Protocol would name a one-sided seam.
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

        ``check`` limits the rollout to ``VALIDATE``, which is what makes ``plan`` and
        the drift check safe on a live fleet.
        """
        fence = self.targets.claim_generation(deployment_id)
        self.targets.plan(deployment_id, edges, fence=fence)
        phases = self._phases(check)
        # Published once per deployment, before the loop, so even an empty fleet has
        # the release's desired state on disk to inspect.
        self.publish(release)

        last_run: AnsibleRun | None = None
        for target in self.targets.targets(deployment_id):
            outcome: str | None = None
            if target.status is TargetStatus.SUCCEEDED:
                if check:
                    continue
                try:
                    self._verify(target.edge, release)
                except Exception as exc:
                    _LOGGER.exception("recovery verification failed on %s", target.edge)
                    outcome = self._fail(
                        deployment_id,
                        target.edge,
                        fence,
                        TargetPhase.VERIFY,
                        f"recovery observation failed: {exc}",
                    )
                else:
                    continue
            else:
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
        """One edge through every phase; returns the failure, or ``None``.

        Every phase is idempotent, so a resumed edge simply re-runs its interrupted one.
        """
        if not self.targets.begin(
            deployment_id, target.edge, fence=fence, now=self.clock()
        ):
            # The fence moved: another worker owns this rollout now.
            return "the rollout was taken over by another worker", None

        last_run: AnsibleRun | None = None
        for phase in phases:
            if target.phase is not None and _already_done(target.phase, phase):
                continue
            try:
                produced = self._run_phase(phase, target.edge, release, check=check)
            except ExecutionError as exc:
                # Ansible could not run at all: a controller fault, not this
                # edge's. Close the row and re-raise to fail the deployment.
                detail = self._withdraw(phase, target.edge, str(exc), check=check)
                self._fail(
                    deployment_id,
                    target.edge,
                    fence,
                    phase,
                    detail,
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
                    self._withdraw(
                        phase, target.edge, f"{type(exc).__name__}: {exc}", check=check
                    ),
                ), last_run
            # VERIFY's commit result must not clear a failed activation's report.
            last_run = produced or last_run
            if produced is not None and not produced.succeeded:
                return self._fail(
                    deployment_id,
                    target.edge,
                    fence,
                    phase,
                    self._withdraw(
                        phase,
                        target.edge,
                        produced.summary() or "the run reported a failure",
                        check=check,
                    ),
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

        VERIFY observes first, then commits the activation with a tool run.
        Observation failures raise; a failed tool result is reported, not raised.
        """
        if phase is TargetPhase.PREPARE:
            # Shared work done once in `converge`, still recorded on every target.
            return None
        if phase is TargetPhase.VALIDATE:
            return self.runner.run(check=True, host_limit=edge)
        if phase is TargetPhase.STAGE:
            return self.runner.run(check=False, host_limit=edge, tags=_STAGE_TAGS)
        if phase is TargetPhase.ACTIVATE:
            return self.runner.run(check=check, host_limit=edge, tags=("activate",))
        self._verify(edge, release)
        # Only an external observation may commit the edge-local journal.
        # Replaying this after a lost reply is safe: commit is idempotent.
        return self.runner.run(check=False, host_limit=edge, tags=("commit",))

    def _withdraw(
        self, phase: TargetPhase, edge: str, detail: str, *, check: bool
    ) -> str:
        """Withdraw an uncommitted activation, keeping both failure reports.

        The edge's journal holds the previous state, so this works across a worker
        restart. A failed withdrawal is never reported as a restored service.
        """
        if check or phase not in (TargetPhase.ACTIVATE, TargetPhase.VERIFY):
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


def _already_done(reached: TargetPhase, phase: TargetPhase) -> bool:
    """Whether a resumed edge has already completed this phase, by phase order."""
    from blitzecdn.capabilities.deployments.domain import PHASE_ORDER

    return PHASE_ORDER.index(phase) <= PHASE_ORDER.index(reached)
