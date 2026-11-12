"""Which queued resource each client ``Idempotency-Key`` created.

A key is reserved before its request creates anything and bound to the
resource once that exists, so a retry finds either the resource or a request
still in progress. Keys are per operator and forgotten after a day.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, or_

from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.persistence.tables import IdempotencyKeyRow
from blitzecdn.core.ports.idempotency import KeyRecord

__all__ = ["KEY_LIFETIME", "RESERVATION_TIMEOUT", "IdempotencyKeys"]

#: How long a key keeps answering with the resource it created.
KEY_LIFETIME = timedelta(hours=24)

#: How long a reservation never bound to a resource blocks its key. Creating a
#: queued resource takes moments; one older than this belongs to a request
#: whose process stopped part way.
RESERVATION_TIMEOUT = timedelta(minutes=5)


class IdempotencyKeys:
    """The ``idempotency_keys`` table, behind ``IdempotencyKeyStore``."""

    def __init__(
        self,
        database: Database,
        *,
        lifetime: timedelta = KEY_LIFETIME,
        reservation_timeout: timedelta = RESERVATION_TIMEOUT,
    ) -> None:
        self._db = database
        self._lifetime = lifetime
        self._reservation_timeout = reservation_timeout

    def reserve(self, operator: str, key: str, fingerprint: str) -> KeyRecord | None:
        """Reserve ``key`` for this request, or return what already holds it.

        Expired keys are dropped first, so an expired key reserves afresh.
        """
        with self._db.writer() as session:
            now = self._db.now()
            # Inline, so expiry cannot silently stop.
            session.execute(
                delete(IdempotencyKeyRow).where(
                    or_(
                        IdempotencyKeyRow.created_at < now - self._lifetime,
                        IdempotencyKeyRow.resource_id.is_(None)
                        & (
                            IdempotencyKeyRow.created_at
                            < now - self._reservation_timeout
                        ),
                    )
                )
            )
            row = session.get(IdempotencyKeyRow, (operator, key))
            if row is not None:
                return KeyRecord(
                    fingerprint=row.fingerprint, resource_id=row.resource_id
                )
            session.add(
                IdempotencyKeyRow(
                    operator=operator,
                    key=key,
                    fingerprint=fingerprint,
                    resource_id=None,
                    created_at=now,
                )
            )
            return None

    def bind(self, operator: str, key: str, resource_id: str) -> None:
        """Record the resource the reserving request created."""
        with self._db.writer() as session:
            row = session.get(IdempotencyKeyRow, (operator, key))
            if row is not None:
                row.resource_id = resource_id

    def release(self, operator: str, key: str) -> None:
        """Drop a reservation whose request created nothing, so a retry may."""
        with self._db.writer() as session:
            session.execute(
                delete(IdempotencyKeyRow).where(
                    IdempotencyKeyRow.operator == operator,
                    IdempotencyKeyRow.key == key,
                    IdempotencyKeyRow.resource_id.is_(None),
                )
            )
