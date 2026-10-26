from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from blitzecdn.core.domain.audit import AuditEvent
from blitzecdn.core.domain.events import DomainEvent
from blitzecdn.core.domain.runs import AnsibleRun


class AuditTrail(Protocol):
    """The append-only operator log, read-only; services write via `EventRecorder`."""

    def get_audit_event(self, event_id: int) -> AuditEvent: ...

    def list_audit_events(self, limit: int = 100) -> list[AuditEvent]: ...


class PlaybookRunner(Protocol):
    """Run one named play across the edges in scope, and report structurally.

    The only fleet primitive a capability is given. Core stages variables, expands
    the host limit and applies the timeout, knowing nothing of what the play is
    for. Takes no deployment lock: an operation that waited for a deploy would be
    useless exactly when one is running.
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


class FleetSettings(Protocol):
    """Fleet-wide, non-secret Ansible values an operator overrides by name."""

    def list_settings(self) -> dict[str, Any]: ...

    def set_setting(self, name: str, value: Any) -> None: ...

    def delete_setting(self, name: str) -> None: ...


__all__ = ["AuditTrail", "EventRecorder", "FleetSettings", "PlaybookRunner"]
