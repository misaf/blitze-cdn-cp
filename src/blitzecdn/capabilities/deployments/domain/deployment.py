"""Convergence history, and the drift reading of a check-mode run."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.workflows.domain import WorkflowKind
from blitzecdn.core.domain.identifiers import DeploymentId, Operator
from blitzecdn.core.domain.runs import AnsibleRun, HostRun, RunStatus


class DeploymentRequirementKind(StrEnum):
    """A durable reason the current desired state has to reach the fleet.

    Outlives the process that raised it, so a crash between writing certificate
    material and converging it loses nothing. A closed enum because the service
    and the scheduler must both understand every kind; the value is stored.
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


#: The two journal entry kinds this capability opens.
DEPLOYMENT_WORKFLOW: WorkflowKind = "deployment"
ROLLBACK_WORKFLOW: WorkflowKind = "rollback"

#: The states a deployment can finish in. A terminal deployment is a record of
#: what happened; nothing about it changes again.
TERMINAL_STATUSES = frozenset(
    {
        DeploymentStatus.SUCCEEDED,
        DeploymentStatus.FAILED,
        DeploymentStatus.TIMED_OUT,
        DeploymentStatus.ABANDONED,
    }
)

#: The lawful transitions; anything else is a programming error.
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


def aborted_run(exc: BaseException, *, interrupted: bool, now: datetime) -> AnsibleRun:
    """A result for a deployment whose runner raised before Ansible reported anything.

    Recorded in the same shape as any other outcome so readers handle one form.
    """
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


def abandoned_run(reason: str, *, now: datetime) -> AnsibleRun:
    """A result for a deployment whose process died, in :func:`aborted_run`'s shape."""
    return AnsibleRun(
        id=uuid4().hex,
        playbook="",
        status=RunStatus.UNSTARTED,
        return_code=None,
        started_at=now,
        finished_at=now,
        error=reason,
    )


def is_terminal(status: DeploymentStatus) -> bool:
    """Whether ``status`` is a finished state no transition leaves."""
    return status in TERMINAL_STATUSES


def require_transition(current: DeploymentStatus, target: DeploymentStatus) -> None:
    """Refuse a transition the lifecycle does not contain, with ``ValueError``.

    A programming error, unlike ``ConflictError``, which is a race over a lawful
    step.
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
    #: The compiled release this run converges, by short digest; equal state is
    #: an equal id, so "did anything change" is a string comparison.
    release_id: str
    #: Host pattern this run was narrowed to, or ``None`` for every edge. A green
    #: canary is not a converged fleet, so rollback selection skips limited runs.
    host_limit: str | None = None
    rollback_of: str | None = None
    #: For a rollback: the digest of canonical inputs it started from; adoption
    #: refuses if state has moved since.
    canonical_digest: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: What Ansible reported, or ``None`` while queued or running.
    result: AnsibleRun | None = None
    #: A rollout or verification failure independent of Ansible's exit code.
    error: str | None = None

    def start(self, *, now: datetime) -> Deployment:
        """This deployment, running from ``now``; the store refuses a moved row."""
        require_transition(self.status, DeploymentStatus.RUNNING)
        return self.model_copy(
            update={"status": DeploymentStatus.RUNNING, "started_at": now}
        )

    def finish(
        self,
        status: DeploymentStatus,
        *,
        now: datetime,
        result: AnsibleRun | None = None,
        error: str | None = None,
    ) -> Deployment:
        """This deployment, ended in ``status`` at ``now`` with what it learned."""
        if not is_terminal(status):
            raise ValueError(f"{status.value} is not a finished state")
        require_transition(self.status, status)
        return self.model_copy(
            update={
                "status": status,
                "finished_at": now,
                "result": result,
                "error": error,
            }
        )

    @property
    def hosts(self) -> tuple[HostRun, ...]:
        return self.result.hosts if self.result else ()

    @property
    def unattempted(self) -> tuple[str, ...]:
        """Edges this deployment aimed at and never reached, still on the old config."""
        return self.result.unattempted if self.result else ()

    @property
    def detail(self) -> str | None:
        """Why this deployment ended the way it did, in one line."""
        return self.error or (self.result.summary() if self.result else None)

    @property
    def abandoned(self) -> bool:
        """Whether this ended because whoever was converging it went away."""
        return self.status is DeploymentStatus.ABANDONED


class DriftReport(BaseModel):
    """Whether the fleet still matches the state the control plane declares.

    Derived from a check-mode run, so ``changed`` counts tasks that would act, each
    named. Unreachable hosts are reported as such, not as drift, and tasks a role
    skips under check mode go uncounted: this undercounts, never invents.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment_id: str
    checked_at: datetime
    host_limit: str | None = None
    hosts: tuple[HostRun, ...] = ()
    #: Edges the check never got to, and therefore learned nothing about.
    unattempted: tuple[str, ...] = ()

    @classmethod
    def of(
        cls, deployment: Deployment, unattempted: tuple[str, ...] = ()
    ) -> DriftReport:
        """Read a recorded check-mode deployment as a drift report.

        ``unattempted`` comes from the target rows the caller holds, since a run's own
        host list cannot know which edges a stopped rollout never reached.
        """
        return cls(
            deployment_id=deployment.id,
            checked_at=deployment.finished_at or deployment.created_at,
            host_limit=deployment.host_limit,
            hosts=deployment.hosts,
            unattempted=tuple(dict.fromkeys((*deployment.unattempted, *unattempted))),
        )

    @property
    def in_sync(self) -> bool:
        """True only if every targeted edge was reached and none would change."""
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
