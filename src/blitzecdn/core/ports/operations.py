from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from blitzecdn.core.domain.audit import AuditEvent
from blitzecdn.core.domain.events import DomainEvent
from blitzecdn.core.domain.runs import AnsibleRun


class AuditTrail(Protocol):
    """The append-only operator log, as the entry layers need to read it.

    Read-only on purpose. Application services write through
    :class:`EventRecorder`; entry layers cannot manufacture audit rows for
    actions no use case performed.
    """

    def get_audit_event(self, event_id: int) -> AuditEvent: ...

    def list_audit_events(self, limit: int = 100) -> list[AuditEvent]: ...


class PlaybookRunner(Protocol):
    """Run one named play across the edges in scope, and report structurally.

    Published as ``ControlPlane.fleet``, and the only fleet capability an
    installed distribution is given. Generic on purpose: core stages the
    variables file, expands a host limit against the fleet it records, applies
    the timeout, and learns nothing about what the play is for. A capability
    that this repository has never heard of runs its own play through exactly
    this method, and one that ships inside it — `cache` — is not privileged.

    Takes no deployment lock. Everything reached this way is an operation
    rather than a convergence: it writes no desired state, and an operation
    that had to wait for a deploy would be useless precisely when a deploy is
    running.
    """

    def run_playbook(
        self,
        *,
        name: str,
        playbook: Path,
        variables: Mapping[str, object],
        host_limit: str | None = None,
    ) -> AnsibleRun: ...


class EventRecorder(Protocol):
    """Durably record one application event in the surrounding transaction."""

    def record(self, event: DomainEvent) -> None: ...


__all__ = ["AuditTrail", "EventRecorder", "PlaybookRunner"]
