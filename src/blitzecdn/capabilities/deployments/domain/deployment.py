"""Convergence history, and the drift reading of a check-mode run.

A ``Deployment`` is the record: who asked, what snapshot, and — once it has
finished — the :class:`~blitzecdn.core.domain.runs.AnsibleRun` it produced. A
``DriftReport`` is the same run read as a question rather than an instruction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.workflows.domain import WorkflowKind
from blitzecdn.core.domain.identifiers import DeploymentId, Operator
from blitzecdn.core.domain.runs import AnsibleRun, HostRun, RunStatus


class DeploymentRequirementKind(StrEnum):
    """A durable reason the current desired state has to reach the fleet.

    A requirement outlives the process that raised it: certificate material
    written to the control plane is not on an edge until a deployment converges,
    and a crash in between must not lose that fact. The set is closed and small
    — every kind has to be understood by the deployment service that clears it
    and by the scheduler that acts on it — so it is an enum rather than a string
    a caller can invent. The value is what persistence stores, so adding a
    member needs no migration and renaming one does.
    """

    CERTIFICATES = "certificates"


class DeploymentStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    ABANDONED = "abandoned"

    @classmethod
    def of(cls, run: AnsibleRun) -> DeploymentStatus:
        """The lifecycle state a finished run leaves the deployment in."""
        if run.status is RunStatus.TIMED_OUT:
            return cls.TIMED_OUT
        return cls.SUCCEEDED if run.status is RunStatus.SUCCEEDED else cls.FAILED


#: The states a deployment can finish in. A terminal deployment is a record of
#: what happened; nothing about it changes again.
#: The two kinds of journal entry this capability opens, named here because
#: this is the capability that does the work. They were members of a
#: `WorkflowKind` enum in the journal itself, beside `certificate` — which is
#: an optional wheel's work, and the reason the enum could not stay closed
#: without the journal knowing every capability that might ever keep one.
DEPLOYMENT_WORKFLOW: WorkflowKind = "deployment"
ROLLBACK_WORKFLOW: WorkflowKind = "rollback"

TERMINAL_STATUSES = frozenset(
    {
        DeploymentStatus.SUCCEEDED,
        DeploymentStatus.FAILED,
        DeploymentStatus.TIMED_OUT,
        DeploymentStatus.ABANDONED,
    }
)

#: The one table that decides which transitions are real, owned here rather
#: than by whichever store happens to enforce it first. Anything not on the
#: left never changes; anything missing from a row on the right is a
#: programming error, not a state the model can reach.
DEPLOYMENT_TRANSITIONS: dict[DeploymentStatus, frozenset[DeploymentStatus]] = {
    DeploymentStatus.QUEUED: frozenset(
        {
            DeploymentStatus.RUNNING,
            DeploymentStatus.FAILED,
        }
    ),
    DeploymentStatus.RUNNING: frozenset(
        {
            DeploymentStatus.SUCCEEDED,
            DeploymentStatus.FAILED,
            DeploymentStatus.TIMED_OUT,
            DeploymentStatus.ABANDONED,
        }
    ),
}


def aborted_run(exc: BaseException, *, interrupted: bool) -> AnsibleRun:
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


def is_terminal(status: DeploymentStatus) -> bool:
    """Whether ``status`` is a finished state no transition leaves."""
    return status in TERMINAL_STATUSES


def require_transition(current: DeploymentStatus, target: DeploymentStatus) -> None:
    """Refuse a transition the lifecycle does not contain.

    Raises ``ValueError`` for a step that can never happen — a terminal
    deployment changing, or a jump past a required one. This is a programming
    error, distinct from ``ConflictError``, which is a race: two processes
    racing to move the same deployment, where the loser's *expected* state is
    stale even though the step itself is lawful.
    """
    if target not in DEPLOYMENT_TRANSITIONS.get(current, frozenset()):
        raise ValueError(
            f"illegal deployment transition {current.value} -> {target.value}"
        )


class Deployment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: DeploymentId
    status: DeploymentStatus
    operator: Operator
    check_mode: bool
    #: Host pattern this run was narrowed to, or ``None`` for every edge.
    #:
    #: Recorded rather than derived because it changes what a green result
    #: means. A canary that succeeded against one edge is not evidence the
    #: fleet converged, and a rollback targeting it would restore a snapshot
    #: most edges never received — which is why ``successful_rollback_target``
    #: skips limited runs.
    host_limit: str | None = None
    rollback_of: str | None = None
    #: For a rollback: the canonical desired state it started from, as a digest.
    #: Adoption refuses if canonical state no longer matches, because restoring
    #: wholesale over a concurrent record write would delete it silently.
    canonical_digest: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: What Ansible reported, or ``None`` while queued or running. This is the
    #: whole of what the deployment knows about the fleet; the raw output it
    #: names in ``result.log_path`` is for an operator to read, not for code.
    result: AnsibleRun | None = None

    @property
    def hosts(self) -> tuple[HostRun, ...]:
        return self.result.hosts if self.result else ()

    @property
    def unattempted(self) -> tuple[str, ...]:
        """Edges this deployment aimed at and never reached.

        A failed fleet deploy stops at the batch that failed, so the edges after
        it keep their previous configuration. That is the difference between
        "the deploy failed" and "the fleet is now running two configurations",
        and it is not visible anywhere in ``hosts``.
        """
        return self.result.unattempted if self.result else ()

    @property
    def detail(self) -> str | None:
        """Why this deployment ended the way it did, in one line."""
        return self.result.summary() if self.result else None


class DriftReport(BaseModel):
    """Whether the fleet still matches the state the control plane declares.

    Deploy answers "make it so"; this answers "is it still so". It is derived
    from a check-mode run, so ``changed`` counts tasks that *would* act, not
    tasks that did — and each host names them, so an operator sees which
    configuration moved rather than only how many tasks would run.

    Two honest limits on reading it. A host that is unreachable is reported as
    such rather than as drift — we did not learn anything about its
    configuration. And a task the role skips under check mode cannot be
    counted, so this floors rather than exactly measures the difference: it
    reliably tells you drift exists, and undercounts rather than invents it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment_id: str
    checked_at: datetime
    host_limit: str | None = None
    hosts: tuple[HostRun, ...] = ()
    #: Edges the check never got to, and therefore learned nothing about.
    unattempted: tuple[str, ...] = ()

    @classmethod
    def of(cls, deployment: Deployment) -> DriftReport:
        return cls(
            deployment_id=deployment.id,
            checked_at=deployment.finished_at or deployment.created_at,
            host_limit=deployment.host_limit,
            hosts=deployment.hosts,
            unattempted=deployment.unattempted,
        )

    @property
    def in_sync(self) -> bool:
        """True only if every targeted host was reached and none would change.

        An unattempted edge makes this false rather than being ignored. The
        question is whether the fleet matches desired state, and an edge the
        check never got to is one we have no answer for — reporting in sync on
        the strength of the batch that did run would be answering a narrower
        question than the one asked.
        """
        return (
            bool(self.hosts)
            and not self.unattempted
            and all(host.in_sync for host in self.hosts)
        )

    @property
    def drifted(self) -> tuple[HostRun, ...]:
        return tuple(host for host in self.hosts if host.changed and not host.failed)

    @property
    def unreachable(self) -> tuple[HostRun, ...]:
        return tuple(host for host in self.hosts if not host.reached)
