"""What one Ansible invocation did, as the control plane understands it.

Every decision about a run is made from these models, populated from Runner's
structured events. Ansible's human-facing output is not an interface: it can't
tell a task failure from a failed start, or say which tasks changed. It is kept
only as a log for operators.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class TaskOutcome(StrEnum):
    """What became of one task on one host."""

    OK = "ok"
    CHANGED = "changed"
    FAILED = "failed"
    UNREACHABLE = "unreachable"
    SKIPPED = "skipped"


class TaskResult(BaseModel):
    """One changed or failed task on one host; tasks that did nothing are not kept."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str
    #: The module the task ran, e.g. ``ansible.builtin.template``. Useful when
    #: two roles name a task the same thing.
    action: str = ""
    outcome: TaskOutcome
    #: The failure message Ansible produced, trimmed. Absent for a change.
    message: str | None = None
    #: The role the task came from, when Ansible knows it.
    role: str | None = None


class HostRun(BaseModel):
    """One edge's part in a run; under ``--check`` the counts are what would happen."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    ok: int = 0
    changed: int = 0
    failed: int = 0
    unreachable: int = 0
    skipped: int = 0
    rescued: int = 0
    ignored: int = 0
    #: The tasks behind the ``changed`` count, so a report can say what changes.
    changes: tuple[TaskResult, ...] = ()
    failures: tuple[TaskResult, ...] = ()
    #: Structured data a role returned by setting the ``blitzecdn_report`` fact.
    report: dict[str, object] | None = None

    @property
    def reached(self) -> bool:
        """Whether Ansible got as far as running tasks here."""
        return self.unreachable == 0

    @property
    def succeeded(self) -> bool:
        return self.failed == 0 and self.unreachable == 0

    @property
    def in_sync(self) -> bool:
        """Converged and reachable: nothing would change and nothing failed."""
        return self.changed == 0 and self.succeeded


class RunStatus(StrEnum):
    """The outcome of the invocation as a whole."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: Stopped by Ansible Runner at ``deployment_timeout_seconds``. Whatever it
    #: had already changed on an edge stays changed.
    TIMED_OUT = "timed_out"
    #: Ansible could not be executed at all; unlike ``FAILED``, the fault is the
    #: controller's installation.
    UNSTARTED = "unstarted"


class AnsibleRun(BaseModel):
    """The authoritative result of one invocation.

    ``hosts`` comes from Runner events, ``status`` and ``return_code`` from the
    process, because a run can die before emitting any host event.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    playbook: str
    status: RunStatus
    return_code: int | None = None
    started_at: datetime
    finished_at: datetime
    hosts: tuple[HostRun, ...] = ()
    #: The edges this run was aimed at, resolved before it started. The edge
    #: play is ``serial`` with ``any_errors_fatal``, so batches after a failure
    #: never appear in ``hosts``; this is how a split fleet is detected.
    targeted: tuple[str, ...] = ()
    #: Where the raw output was kept. Never parsed; shown to an operator, and
    #: named in errors so the next question has somewhere to go.
    log_path: str | None = None
    #: Process-level detail for a run that produced no structured output.
    error: str | None = None

    @property
    def reported(self) -> bool:
        """Whether Runner produced per-host results.

        False for ``--syntax-check``; otherwise false means the run died early and the
        log is the account.
        """
        return bool(self.hosts)

    @property
    def succeeded(self) -> bool:
        return self.status is RunStatus.SUCCEEDED

    @property
    def failed_hosts(self) -> tuple[HostRun, ...]:
        return tuple(host for host in self.hosts if not host.succeeded)

    @property
    def unreachable_hosts(self) -> tuple[HostRun, ...]:
        return tuple(host for host in self.hosts if not host.reached)

    @property
    def changed_hosts(self) -> tuple[HostRun, ...]:
        """Hosts that changed — or under check mode, that would change."""
        return tuple(host for host in self.hosts if host.changed and not host.failed)

    @property
    def unattempted(self) -> tuple[str, ...]:
        """Edges the run aimed at that never reported, because the play stopped first.

        Unlike unreachable hosts these never appear in ``hosts``. Empty when nothing
        reported at all; ask ``reported`` then.
        """
        if not self.hosts:
            return ()
        reported = {host.host for host in self.hosts}
        return tuple(name for name in self.targeted if name not in reported)

    def host(self, name: str) -> HostRun | None:
        return next((host for host in self.hosts if host.host == name), None)

    def summary(self) -> str:
        """One actionable line: the first real failure message, else the return code."""
        for host in self.failed_hosts:
            for failure in host.failures:
                detail = (failure.message or failure.outcome.value).strip()
                return f"{host.host}: {failure.task}: {detail}{self._remainder()}"
        if self.error:
            return self.error
        return f"ansible-playbook exited {self.return_code}{self._remainder()}"

    def _remainder(self) -> str:
        """Name the edges the play never got to: the fleet is now split."""
        remaining = self.unattempted
        if not remaining:
            return ""
        return (
            f" {len(remaining)} edge(s) were never attempted and still have "
            f"their previous configuration: {', '.join(remaining)}."
        )

    def unreported(self, what: str) -> str:
        """Explain a run with no per-host result: what the process said, and the log."""
        location = f" The full output is at {self.log_path}." if self.log_path else ""
        detail = self.error or self.summary()
        return f"no edge reported a {what} result. {detail}{location}"
