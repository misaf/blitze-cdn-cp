"""What a GeoIP refresh needs from the controller around it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol

from blitzecdn.capabilities.security.geoip.domain import GeoIpDatabase

__all__ = [
    "DatabaseSource",
    "DatabaseStore",
    "DeploymentLock",
    "DeploymentRequirement",
    "ReleaseHistory",
]


class DatabaseSource(Protocol):
    """Where a fresh database comes from: MaxMind, once, on the controller."""

    def download(self) -> bytes:
        """The current database's bytes."""
        ...


class DatabaseStore(Protocol):
    """Immutable, fingerprinted databases, and which one is current."""

    def current(self) -> GeoIpDatabase | None: ...

    def current_digest(self) -> str | None:
        """The digest ``current`` records, whether or not its file still exists."""
        ...

    def install(self, payload: bytes) -> GeoIpDatabase:
        """Store ``payload`` under its digest and make it current."""
        ...

    def collect(self, keep: frozenset[str]) -> int:
        """Remove every stored database whose digest is not in ``keep``."""
        ...


class DeploymentLock(Protocol):
    """The fleet-wide deployment lock: no release compiles while it is held."""

    def lock(self) -> AbstractContextManager[Any]: ...


class DeploymentRequirement(Protocol):
    """Records, durably, that the fleet has to receive a new database."""

    def __call__(self) -> None: ...


class ReleasedDocument(Protocol):
    @property
    def document(self) -> Mapping[str, Any]: ...


class ReleasedState(Protocol):
    @property
    def artifacts(self) -> Sequence[ReleasedDocument]: ...


class ReleaseHistory(Protocol):
    """Every retained release, whose pinned database a rollback still needs."""

    def list_releases(self, limit: int | None = 20) -> Sequence[ReleasedState]: ...
