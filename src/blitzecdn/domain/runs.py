"""What one Ansible invocation did, as the control plane understands it.

Every application decision about a run — did it converge, which edges drifted,
which host still serves a purged object, whether an edge is safe to forget — is
made from these models and from nothing else.

They are populated from Ansible Runner's structured event stream. The
``blitzecdn_result`` callback also writes a per-invocation JSON document as a
compatibility fallback. Raw Ansible output goes to a per-run log file that no
application code reads: it exists for an operator, and for the one case
structured output cannot cover — a process that died before Ansible could
report anything.

The reason for the split is that Ansible's human-facing output is not an
interface. Reading the desired state back out of ``PLAY RECAP`` meant a run
that failed *inside* a task and a run that failed to start looked similar; it
could count how many tasks would change but never say which; and any release
that reworded a line would have quietly changed what the control plane
believed. Runner events expose the task objects themselves.
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
    #: Stopped by Ansible Runner at ``deployment_timeout_seconds``. Whatever it
    #: had already changed on an edge stays changed.
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
    #: The edges this run was aimed at, resolved before it started.
    #:
    #: Recorded because ``hosts`` alone cannot answer "what about the rest of
    #: the fleet". The edge play runs ``serial`` with ``any_errors_fatal``, so a
    #: failure in one batch stops the play and every later batch is never
    #: contacted — those hosts do not fail, they simply never appear, and the
    #: result reads as though the fleet were smaller than it is. That is the
    #: worst moment to be guessing: some edges are now on the new configuration
    #: and some on the old, and which is which is exactly this difference.
    #:
    #: Empty for a run with no host expansion to record, such as
    #: ``--syntax-check``.
    targeted: tuple[str, ...] = ()
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

    @property
    def unattempted(self) -> tuple[str, ...]:
        """Edges this run was aimed at that never reported anything.

        Not the same as unreachable. An unreachable host was contacted and
        failed to answer, and Ansible says so; these were never reached at all,
        because the play stopped before their batch. Both leave an edge on its
        previous configuration, but only one of them is visible in ``hosts``.

        Empty when nothing was reported at all — a run that died before any
        host ran has not left part of the fleet behind, it has done nothing,
        and ``reported`` is the right question there.
        """
        if not self.hosts:
            return ()
        reported = {host.host for host in self.hosts}
        return tuple(name for name in self.targeted if name not in reported)

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
                return f"{host.host}: {failure.task}: {detail}{self._remainder()}"
        if self.error:
            return self.error
        return f"ansible-playbook exited {self.return_code}{self._remainder()}"

    def _remainder(self) -> str:
        """Name the edges the play never got to, when there are any.

        Appended to the failure rather than reported separately because the two
        facts are only useful together: "edge-b failed" invites a fix on edge-b,
        while "edge-b failed and edge-c, edge-d were never attempted" says the
        fleet is now split across two configurations.
        """
        remaining = self.unattempted
        if not remaining:
            return ""
        return (
            f" {len(remaining)} edge(s) were never attempted and still have "
            f"their previous configuration: {', '.join(remaining)}."
        )

    def unreported(self, what: str) -> str:
        """Explain a run that produced no per-host result at all.

        Every fleet operation that answers from ``hosts`` needs this, and the
        honest answer is the same for each: nothing came back, here is what the
        process said about itself, and here is where the output is. It belongs
        on the run because it is only ever a reading of one — and because the
        services that need it are no longer in the same module, which is how it
        would otherwise have become several accounts of the same silence.
        """
        location = f" The full output is at {self.log_path}." if self.log_path else ""
        detail = self.error or self.summary()
        return f"no edge reported a {what} result. {detail}{location}"
