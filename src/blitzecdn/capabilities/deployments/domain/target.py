"""One edge's progress through a deployment, as a durable row.

The rollout is per edge and each edge's progress is a row. A single
fleet-wide Ansible run would keep which edges it had reached in memory until it
ended, so a controller restarted mid-rollout would know nothing about which
edges already serve the new configuration. Rows buy three things.

**Partial failure is explicit.** Six edges succeeded, one failed, three were
never attempted — and each of those is a status on a row rather than an
inference from counters. A retry re-runs the three, not the ten.

**Recovery is resumption.** A controller that dies mid-rollout leaves rows
saying exactly which edges reached which phase, and the deployment continues
from there rather than starting again.

**Progress is observable while it happens.** An operator watching a fleet
converge can read the table.

Phases
------
The five are ordered and each is genuinely separable work.

``PREPARE``
    The release is compiled and its artifact published where Ansible will read
    it. Once per deployment, not per edge — but recorded per edge anyway, so a
    target's row tells its whole story without joining anything.
``VALIDATE``
    A check-mode run against this edge. Reports what would change and changes
    nothing.
``STAGE``
    Prepare the runtime, render configuration and executable resources into a
    release directory, test them with the exact selected image, and seal their
    content. The active configuration pointer is not changed.
``ACTIVATE``
    Journal the previous pointer and runtime records, atomically select the
    sealed tree, then start or reload Nginx. Failure restores the previous
    configuration and runtime definition.
``VERIFY``
    Ask the edge, over the network, whether it is actually serving what the
    release says it should. Deliberately last and deliberately not Ansible's
    return code — see :class:`TargetPhase`. Commit the activation journal only
    after that observation; withdraw the pending activation if it fails.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

__all__ = ["PHASE_ORDER", "DeploymentTarget", "TargetPhase", "TargetStatus"]


class TargetPhase(StrEnum):
    """How far one edge has got, and what each step actually proves.

    ``VERIFY`` is the one worth dwelling on. A successful ``ACTIVATE`` means
    Ansible's tasks reported ok and ``nginx -s reload`` returned zero. Neither
    is evidence that a visitor gets a response: a reload that nginx accepts can
    still leave an upstream unreachable, a listener unclaimed, a certificate
    unreadable by the worker, or a firewall closed in front of the whole thing.
    Treating "the tool said ok" as "the fleet is serving" is the specific
    mistake this phase exists to refuse, so verification is a request made to
    the edge from outside it, and a deployment that reaches ``ACTIVATE`` and
    fails ``VERIFY`` is a failed deployment.
    """

    PREPARE = "prepare"
    VALIDATE = "validate"
    STAGE = "stage"
    ACTIVATE = "activate"
    VERIFY = "verify"


#: The order the phases are attempted in. A list rather than the enum's own
#: order, because "which comes next" is a rule and an enum's declaration order
#: is an accident of how somebody typed it.
PHASE_ORDER: tuple[TargetPhase, ...] = (
    TargetPhase.PREPARE,
    TargetPhase.VALIDATE,
    TargetPhase.STAGE,
    TargetPhase.ACTIVATE,
    TargetPhase.VERIFY,
)


class TargetStatus(StrEnum):
    """Where one edge stands in this deployment."""

    #: Aimed at, not yet touched. The status that makes "three edges were never
    #: attempted" a fact rather than an inference.
    PENDING = "pending"
    RUNNING = "running"
    #: Every phase passed, verification included.
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: The rollout stopped before reaching this edge, because an earlier one
    #: failed. Distinct from FAILED: this edge is still serving whatever it
    #: had, and nothing is known to be wrong with it.
    SKIPPED = "skipped"


class DeploymentTarget(BaseModel):
    """One edge in one deployment, and how far it got."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment_id: str
    edge: str
    status: TargetStatus = TargetStatus.PENDING
    #: The last phase this edge *completed*, or ``None`` before it starts.
    #: Completed rather than attempted, so resuming re-runs the phase that was
    #: interrupted — every phase is idempotent, and re-running one is cheaper
    #: than reasoning about how far into it a dead process had got.
    phase: TargetPhase | None = None
    attempts: int = 0
    #: The rollout's generation when this row was last written.
    #:
    #: The fence. A worker that was paused past its job lease comes back
    #: believing it still owns this deployment; the row it tries to advance
    #: names a generation that has moved on, so its write matches nothing. A
    #: lease decides who *may* converge an edge; this decides whose result
    #: about that edge is recorded.
    fence: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None

    @property
    def next_phase(self) -> TargetPhase | None:
        """The phase to attempt now, or ``None`` when this edge is done."""
        if self.phase is None:
            return PHASE_ORDER[0]
        index = PHASE_ORDER.index(self.phase)
        if index + 1 >= len(PHASE_ORDER):
            return None
        return PHASE_ORDER[index + 1]

    @property
    def complete(self) -> bool:
        return self.phase is TargetPhase.VERIFY
