"""Which queued resource each client ``Idempotency-Key`` created.

The port the queued API routes consult; the table behind it is core's own,
since a key may name any capability's resource.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["IdempotencyKeyStore", "KeyRecord"]


@dataclass(frozen=True, slots=True)
class KeyRecord:
    """What a key already holds: the request it came with and what it created."""

    fingerprint: str
    #: ``None`` while the request that reserved the key is still running.
    resource_id: str | None


class IdempotencyKeyStore(Protocol):
    """Reserve a key, bind it to what its request created, or release it."""

    def reserve(self, operator: str, key: str, fingerprint: str) -> KeyRecord | None:
        """Reserve ``key`` for this request, or return what already holds it."""
        ...

    def bind(self, operator: str, key: str, resource_id: str) -> None:
        """Record the resource the reserving request created."""

    def release(self, operator: str, key: str) -> None:
        """Drop a reservation whose request created nothing, so a retry may."""
