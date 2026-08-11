"""The in-process event bus and its subscribers.

The application publishes :class:`~blitzecdn.domain.events.DomainEvent` objects
through the ``EventBus`` port. This module is the only concrete implementation:
a synchronous fan-out to every subscribed observer, in the order they were
registered. Synchronous on purpose — the audit trail has to be durable before
a caller that just created a record is told it was created, and there is no
listener yet that should make a caller wait.

``AuditObserver`` turns each event into the append-only audit row. Keeping the
mapping here, beside the bus, is what lets the application services stop knowing
the audit trail exists at all.
"""

from __future__ import annotations

from collections.abc import Callable

from blitzecdn.domain.events import DomainEvent
from blitzecdn.infrastructure.stores import AuditLog

Subscriber = Callable[[DomainEvent], None]


class InProcessEventBus:
    """Publishes to every subscribed observer, synchronously, in registration order."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def publish(self, event: DomainEvent) -> None:
        for subscriber in self._subscribers:
            subscriber(event)


class AuditObserver:
    """The audit trail, as a listener rather than a dependency of every service.

    ``AuditLog`` is the one consumer today; a second subscriber (metrics,
    alerting) is one more ``subscribe`` call in the composition root.
    """

    def __init__(self, audit_log: AuditLog) -> None:
        self._audit_log = audit_log

    def __call__(self, event: DomainEvent) -> None:
        self._audit_log.audit(
            operator=event.operator,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            details=event.details,
        )
