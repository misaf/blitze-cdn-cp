"""In-process domain events, published by the application services.

An event names what happened — a record was created, a deployment succeeded,
a cache was purged — and is published the moment the application has decided
it, at the boundary where the decision meets the outside world. The service
that publishes it neither knows nor cares who listens: the audit trail is one
subscriber, and a metrics counter or an alerting rule would be another, added
in the composition root without touching the service.

The shape is deliberately the shape of an audit entry, because the audit trail
is the first and for now the only consumer. ``details`` carries everything a
subscriber needs to describe the event without reaching back into the store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DomainEvent(BaseModel):
    """One thing that happened, captured with who asked and what they asked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operator: str
    action: str
    resource_type: str
    resource_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def domain_event(
    operator: str,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> DomainEvent:
    """Build an event the way the audit trail used to be written, field by field."""
    return DomainEvent(
        operator=operator,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details or {},
    )
