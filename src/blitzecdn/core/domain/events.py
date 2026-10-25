"""In-process domain events, published by the application services.

A service publishes what happened without knowing who listens; the audit trail
is the one subscriber today, which is why an event has an audit entry's shape.
"""

from __future__ import annotations

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


def domain_event(
    operator: str,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> DomainEvent:
    """Build an event from the audit trail's own fields, one by one."""
    return DomainEvent(
        operator=operator,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details or {},
    )
