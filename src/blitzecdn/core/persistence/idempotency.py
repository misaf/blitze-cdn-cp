"""Which queued resource each client ``Idempotency-Key`` created.

A key is reserved before its request creates anything and bound in the
transaction that writes the resource, so a retry finds either the resource or a
request still in progress, never a resource its key does not name. Keys are per
operator and forgotten after a day.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, or_

from blitzecdn.core.exceptions import ConflictError
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.persistence.tables import IdempotencyKeyRow
from blitzecdn.core.ports.idempotency import KeyRecord, Reservation

__all__ = ["KEY_LIFETIME", "RESERVATION_TIMEOUT", "IdempotencyKeys"]

#: How long a key keeps answering with the resource it created.
KEY_LIFETIME = timedelta(hours=24)

#: How long a reservation never bound to a resource blocks its key. Creating a
#: queued resource takes moments; one older than this belongs to a request
#: whose process stopped part way, which, since binding commits with the
#: resource, queued nothing.
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

    def reserve(
        self, operator: str, key: str, fingerprint: str
    ) -> Reservation | KeyRecord:
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
            return Reservation(
                operator=operator, key=key, fingerprint=fingerprint, reserved_at=now
            )

    def bind(self, reservation: Reservation, resource_id: str) -> None:
        """Record the resource the reserving request created, as it is written.

        Only inside the transaction that writes the resource, so the two commit
        together. A reservation that expired and was swept is written again,
        already bound; one another request has since taken over is refused, and
        the creation rolls back with it.
        """
        self._db.require_transaction("binding an idempotency key")
        with self._db.writer() as session:
            row = session.get(
                IdempotencyKeyRow, (reservation.operator, reservation.key)
            )
            if row is None:
                session.add(
                    IdempotencyKeyRow(
                        operator=reservation.operator,
                        key=reservation.key,
                        fingerprint=reservation.fingerprint,
                        resource_id=resource_id,
                        created_at=reservation.reserved_at,
                    )
                )
                return
            if row.resource_id is None and _holds(row, reservation):
                row.resource_id = resource_id
                return
            raise ConflictError(
                "this Idempotency-Key was taken over by a retry while this "
                "request was still being admitted; nothing was queued here, so "
                "send the request again with the same key to read the retry's result"
            )

    def release(self, reservation: Reservation) -> None:
        """Drop a reservation whose request created nothing, so a retry may.

        Only this reservation: a later one of the same key is another request's.
        """
        with self._db.writer() as session:
            row = session.get(
                IdempotencyKeyRow, (reservation.operator, reservation.key)
            )
            if row is not None and row.resource_id is None and _holds(row, reservation):
                session.delete(row)


def _holds(row: IdempotencyKeyRow, reservation: Reservation) -> bool:
    """Whether ``row`` is still the reservation ``reservation`` made."""
    return (
        row.fingerprint == reservation.fingerprint
        and row.created_at == reservation.reserved_at
    )
