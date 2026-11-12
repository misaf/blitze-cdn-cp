"""Which queued resource each client ``Idempotency-Key`` created.

The port the queued API routes consult; the table behind it is core's own,
since a key may name any capability's resource.

A key is bound in the same transaction that writes the resource it names. The
route reserves the key and opens a claim with :func:`claiming_key`; the service
that writes the queued resource calls :func:`bind_claimed_key` inside that
write's transaction. The resource and its key therefore commit together or not
at all, so a process that stops between them leaves nothing a retry could
queue a second time.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

__all__ = [
    "IdempotencyClaim",
    "IdempotencyKeyStore",
    "KeyRecord",
    "Reservation",
    "bind_claimed_key",
    "claiming_key",
]


@dataclass(frozen=True, slots=True)
class KeyRecord:
    """What a key already holds: the request it came with and what it created."""

    fingerprint: str
    #: ``None`` while the request that reserved the key is still running.
    resource_id: str | None


@dataclass(frozen=True, slots=True)
class Reservation:
    """A key this request now holds; only its holder may bind or release it.

    ``reserved_at`` tells this reservation from a later one of the same key, made
    after this one expired: a key is reserved afresh only once the earlier
    reservation is older than the timeout, so the two never share an instant.
    """

    operator: str
    key: str
    fingerprint: str
    reserved_at: datetime


class IdempotencyKeyStore(Protocol):
    """Reserve a key, bind it to what its request created, or release it."""

    def reserve(
        self, operator: str, key: str, fingerprint: str
    ) -> Reservation | KeyRecord:
        """Reserve ``key`` for this request, or return what already holds it."""
        ...

    def bind(self, reservation: Reservation, resource_id: str) -> None:
        """Record the resource the reserving request created.

        Runs inside the transaction that writes the resource. Raises
        ``ConflictError`` when another request has since taken the key over, so
        that transaction, and the resource with it, rolls back.
        """

    def release(self, reservation: Reservation) -> None:
        """Drop a reservation whose request created nothing, so a retry may."""


class IdempotencyClaim:
    """The key of the request now running, waiting for the resource it queues."""

    __slots__ = ("_bind", "resource_id")

    def __init__(self, bind: Callable[[str], None]) -> None:
        self._bind = bind
        #: What the key was bound to, once the resource is written.
        self.resource_id: str | None = None


_CLAIM: ContextVar[IdempotencyClaim | None] = ContextVar(
    "blitzecdn_idempotency_claim", default=None
)


@contextmanager
def claiming_key(bind: Callable[[str], None]) -> Iterator[IdempotencyClaim]:
    """While open, the first queued resource written is bound with ``bind``."""
    claim = IdempotencyClaim(bind)
    token = _CLAIM.set(claim)
    try:
        yield claim
    finally:
        _CLAIM.reset(token)


def bind_claimed_key(resource_id: str) -> None:
    """Bind the running request's key, if any, to the resource it queues.

    Call inside the transaction that writes ``resource_id``. Only the first
    resource a request queues is bound; without a key this does nothing.
    """
    claim = _CLAIM.get()
    if claim is None or claim.resource_id is not None:
        return
    claim._bind(resource_id)
    claim.resource_id = resource_id
