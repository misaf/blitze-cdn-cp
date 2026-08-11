"""What one Ansible invocation did, as the control plane understands it.

Every application decision about a run — did it converge, which edges drifted,
which host still serves a purged object, whether an edge is safe to forget — is
made from these models and from nothing else.

They are populated by the ``blitzecdn_result`` callback plugin, which is handed
a path of its own for each invocation and writes a JSON document there. The raw
Ansible output goes to a per-run log file that no application code reads: it
exists for an operator, and for the one case structured output cannot cover —
a process that died before Ansible could report anything.

The reason for the split is that Ansible's human-facing output is not an
interface. Reading the desired state back out of ``PLAY RECAP`` meant a run
that failed *inside* a task and a run that failed to start looked similar; it
could count how many tasks would change but never say which; and any release
that reworded a line would have quietly changed what the control plane
believed. A callback sees the task objects themselves.
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
    """One task, on one host, and what it did there.

    Only the tasks worth keeping are recorded — those that changed something
    and those that failed. A converged fleet produces a run with none of these,
    which is the point: an operator reading a deployment should see what moved,
    not several hundred lines confirming that nothing did.
    """

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
    """One edge's part in a run.

    The counters mirror what Ansible tallies per host. Under ``--check`` they
    describe what *would* happen, which is what makes a check-mode run readable
    as a drift report.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    ok: int = 0
    changed: int = 0
    failed: int = 0
    unreachable: int = 0
    skipped: int = 0
    rescued: int = 0
    ignored: int = 0
    #: The tasks behind the ``changed`` count. Recap parsing could only ever
    #: produce the number; naming them is the difference between "edge-a would
    #: change 3 tasks" and "edge-a would rewrite two vhosts and reload nginx".
    changes: tuple[TaskResult, ...] = ()
    failures: tuple[TaskResult, ...] = ()
    #: Structured data a role chose to return, published by setting the
    #: ``blitzecdn_report`` fact. This is how ``blitzecdn_stats`` hands its
    #: counters back, and the only supported way for a role to return a payload
    #: rather than an outcome.
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
    #: Killed at ``deployment_timeout_seconds``. Its process group went with
    #: it, but whatever it had already changed on an edge stays changed.
    TIMED_OUT = "timed_out"
    #: Ansible could not be executed at all, so there is no exit code to read
    #: and nothing ran on any host. Distinct from ``FAILED`` — which means
    #: Ansible ran and refused — because the fixes are unrelated: this one is
    #: the controller's own installation.
    UNSTARTED = "unstarted"


class AnsibleRun(BaseModel):
    """The authoritative result of one invocation.

    ``hosts`` comes from the callback. ``status`` and ``return_code`` come from
    the process, because a run can die in ways no callback survives to report —
    which is exactly why both halves are kept.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    playbook: str
    status: RunStatus
    return_code: int | None = None
    started_at: datetime
    finished_at: datetime
    hosts: tuple[HostRun, ...] = ()
    #: Where the raw output was kept. Never parsed; shown to an operator, and
    #: named in errors so the next question has somewhere to go.
    log_path: str | None = None
    #: Process-level detail for a run that produced no structured output.
    error: str | None = None

    @property
    def reported(self) -> bool:
        """Whether the callback produced per-host results.

        False for ``--syntax-check``, which runs no play and so has no hosts to
        report — there, the return code is the whole answer. False elsewhere
        means the run died before Ansible finished, and the log is the account.
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

    def host(self, name: str) -> HostRun | None:
        return next((host for host in self.hosts if host.host == name), None)

    def summary(self) -> str:
        """One line an operator can act on, for an error or a log message.

        Prefers the first real failure message over the return code: "nginx -t
        rejected the configuration" is the answer, and "ansible-playbook exited
        2" is only where to start looking.
        """
        for host in self.failed_hosts:
            for failure in host.failures:
                detail = (failure.message or failure.outcome.value).strip()
                return f"{host.host}: {failure.task}: {detail}"
        if self.error:
            return self.error
        return f"ansible-playbook exited {self.return_code}"
