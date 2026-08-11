"""Convergence history, and the drift reading of a check-mode run.

A ``Deployment`` is the record: who asked, what snapshot, and — once it has
finished — the :class:`~blitzecdn.domain.runs.AnsibleRun` it produced. A
``DriftReport`` is the same run read as a question rather than an instruction.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from blitzecdn.domain.runs import AnsibleRun, HostRun, RunStatus


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


class Deployment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    status: DeploymentStatus
    operator: str
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

    @classmethod
    def of(cls, deployment: Deployment) -> DriftReport:
        return cls(
            deployment_id=deployment.id,
            checked_at=deployment.finished_at or deployment.created_at,
            host_limit=deployment.host_limit,
            hosts=deployment.hosts,
        )

    @property
    def in_sync(self) -> bool:
        """True only if every host was reached and none of them would change."""
        return bool(self.hosts) and all(host.in_sync for host in self.hosts)

    @property
    def drifted(self) -> tuple[HostRun, ...]:
        return tuple(host for host in self.hosts if host.changed and not host.failed)

    @property
    def unreachable(self) -> tuple[HostRun, ...]:
        return tuple(host for host in self.hosts if not host.reached)
