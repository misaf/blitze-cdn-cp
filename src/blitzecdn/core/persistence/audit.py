"""Persistence for the append-only audit trail."""

from typing import Any

from sqlalchemy import delete, select
from sqlmodel import col

from blitzecdn.core.domain.audit import AuditEvent
from blitzecdn.core.domain.events import DomainEvent
from blitzecdn.core.exceptions import NotFoundError
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.persistence.tables import AuditEventRow

#: For tests; a control plane passes `Settings.audit_retention`.
_DEFAULT_RETENTION = 100_000


class AuditLog:
    """Append-only record of who did what, trimmed to a bound from the oldest end."""

    def __init__(self, database: Database, retention: int = _DEFAULT_RETENTION) -> None:
        self._db = database
        self._retention = retention

    def record(self, event: DomainEvent) -> None:
        """Persist the event emitted by a completed application action."""
        self.audit(
            operator=event.operator,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            details=event.details,
        )

    def audit(
        self,
        operator: str,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        with self._db.session() as session:
            row = AuditEventRow(
                created_at=self._db.now(),
                operator=operator,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details or {},
            )
            session.add(row)
            session.flush()
            # Inline: a rowid range seek that does nothing on a normal append.
            # Ids only ever leave from the bottom, so "below newest minus the
            # bound" keeps exactly `retention` events.
            event = self._audit_event(row)
            session.execute(
                delete(AuditEventRow).where(
                    col(AuditEventRow.id) <= event.id - self._retention
                )
            )
            return event

    def get_audit_event(self, event_id: int) -> AuditEvent:
        with self._db.session() as session:
            row = session.get(AuditEventRow, event_id)
            if row is None:
                raise NotFoundError(f"audit event {event_id} does not exist")
            return self._audit_event(row)

    def list_audit_events(self, limit: int = 100) -> list[AuditEvent]:
        with self._db.session() as session:
            rows = session.scalars(
                select(AuditEventRow)
                .order_by(col(AuditEventRow.id).desc())
                .limit(limit)
            ).all()
            return [self._audit_event(row) for row in rows]

    @staticmethod
    def _audit_event(row: AuditEventRow) -> AuditEvent:
        return AuditEvent.model_validate(
            {
                "id": row.id,
                "created_at": row.created_at,
                "operator": row.operator,
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "details": row.details,
            }
        )


__all__ = ["AuditLog"]
