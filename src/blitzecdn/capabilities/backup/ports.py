"""What backing up and restoring needs, split by what could be replaced."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from blitzecdn.capabilities.backup.domain import BackupComponent


class ArchiveEntry(Protocol):
    """One member of an archive, as the safety rules need to see it."""

    @property
    def name(self) -> str: ...

    @property
    def is_link(self) -> bool: ...

    @property
    def link_target(self) -> str | None: ...


class ArchiveGateway(Protocol):
    """The container format: list entries for checking, then extract."""

    def write(self, source: Path, destination: Path) -> None: ...

    def entries(self, archive: Path) -> Sequence[ArchiveEntry]: ...

    def read_member(self, archive: Path, name: str) -> bytes: ...

    def extract(self, archive: Path, destination: Path) -> None: ...


class BackupComponentGateway(Protocol):
    """One component's storage; every ``validate`` precedes any ``restore``."""

    @property
    def component(self) -> BackupComponent: ...

    @property
    def requires_offline(self) -> bool:
        """Whether restoring this needs the control-plane services stopped."""
        ...

    def present(self) -> bool: ...

    def export(self, staging: Path) -> None: ...

    def validate(self, staging: Path) -> None: ...

    def restore(self, staging: Path) -> None: ...

    def recovery_paths(self) -> Sequence[Path]: ...


class RestoreRecovery(Protocol):
    """Preserve selected live paths and recover them if application fails."""

    def protect(self, paths: Sequence[Path]) -> AbstractContextManager[None]: ...


class SchemaVersions(Protocol):
    """Schema identity and preparation of a database before publication."""

    def current(self) -> str | None: ...

    def of(self, path: Path) -> str | None: ...

    def known(self, revision: str) -> bool: ...

    def migrate(self, path: Path) -> None:
        """Upgrade and verify a staged database without touching live state."""
        ...


class ServiceControl(Protocol):
    """Stop the control-plane services for a block; the host owns the restarts."""

    def stopped(self) -> AbstractContextManager[None]: ...


class Workspace(Protocol):
    """A private directory that is removed however the caller leaves it."""

    def scratch(self, prefix: str) -> AbstractContextManager[Path]: ...


__all__ = [
    "ArchiveEntry",
    "ArchiveGateway",
    "BackupComponentGateway",
    "RestoreRecovery",
    "SchemaVersions",
    "ServiceControl",
    "Workspace",
]
