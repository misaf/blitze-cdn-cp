from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from blitzecdn.core.domain.audit import AuditEvent
from blitzecdn.core.domain.events import DomainEvent
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.domain.runs import AnsibleRun


class AuditTrail(Protocol):
    """The append-only operator log, read-only; services write via `EventRecorder`."""

    def get_audit_event(self, event_id: int) -> AuditEvent:
        """One event; raises ``NotFoundError`` if there is none by that id."""
        ...

    def paginate_audit_events(
        self, *, limit: int, offset: int
    ) -> PageResult[AuditEvent]:
        """One page of the log, newest first."""
        ...

    def list_audit_events(self, limit: int = 100) -> list[AuditEvent]:
        """The ``limit`` most recent events, newest first."""
        ...


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
        timeout_seconds: int | None = None,
    ) -> AnsibleRun:
        """Run ``playbook`` across the edges ``host_limit`` selects, or all of them.

        ``variables`` are staged for this run alone and ``name`` labels its log.
        The run is stopped at ``timeout_seconds`` or the deployment timeout,
        whichever is sooner.
        """
        ...


class EventRecorder(Protocol):
    """Durably record one application event in the surrounding transaction."""

    def record(self, event: DomainEvent) -> None:
        """Append ``event`` to the audit log, inside the caller's transaction."""
        ...


class FleetSettings(Protocol):
    """Fleet-wide, non-secret Ansible values an operator overrides by name."""

    def list_settings(self) -> dict[str, Any]:
        """Every overridden setting, by name."""
        ...

    def set_setting(self, name: str, value: Any) -> None:
        """Override ``name`` with ``value``; raises ``ValueError`` for a bad name."""
        ...

    def delete_setting(self, name: str) -> None:
        """Drop the override of ``name``; raises ``NotFoundError`` if there is none."""
        ...


__all__ = ["AuditTrail", "EventRecorder", "FleetSettings", "PlaybookRunner"]
