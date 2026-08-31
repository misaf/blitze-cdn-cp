"""What backing up and restoring needs from the outside world.

Four narrow capabilities, split by what could plausibly be replaced: the
archive container, the per-component storage, somewhere private to stage files,
and the ability to take the controller offline for the part of a restore that
needs it. A single "backup adapter" would have hidden the fact that a TLS-only
restore needs none of the last one.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from blitzecdn.features.backup.domain import BackupComponent


class ArchiveEntry(Protocol):
    """One member of an archive, as the safety rules need to see it."""

    @property
    def name(self) -> str: ...

    @property
    def is_link(self) -> bool: ...

    @property
    def link_target(self) -> str | None: ...


class ArchiveGateway(Protocol):
    """The container format, listed before it is trusted and only then read.

    ``entries`` deliberately comes before ``extract``: every rule in
    :func:`blitzecdn.features.backup.domain.unsafe_member` has to be applied to
    the whole archive while nothing has been written, and an adapter that could only
    extract would make that impossible to express.
    """

    def write(self, source: Path, destination: Path) -> None: ...

    def entries(self, archive: Path) -> Sequence[ArchiveEntry]: ...

    def read_member(self, archive: Path, name: str) -> bytes: ...

    def extract(self, archive: Path, destination: Path) -> None: ...


class BackupComponentGateway(Protocol):
    """One component's storage: whether it exists, and how it moves.

    ``validate`` is separate from ``restore`` because the whole archive must be
    known good before any of it is applied. A component that discovered its own
    missing file halfway through a restore would have already replaced the
    ones ahead of it in the list.
    """

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


class SchemaVersions(Protocol):
    """The database's schema identity, for recording and for compatibility."""

    def current(self) -> str | None: ...

    def of(self, path: Path) -> str | None: ...

    def known(self, revision: str) -> bool: ...


class ServiceControl(Protocol):
    """Stop the control-plane services for the duration of a block.

    A context manager rather than a stop/start pair so the services come back
    whether the restore succeeded, failed, or raised — leaving a controller
    stopped because a restore threw is the failure this shape prevents.
    """

    def stopped(self) -> AbstractContextManager[None]: ...


class Workspace(Protocol):
    """A private directory that is removed however the caller leaves it."""

    def scratch(self, prefix: str) -> AbstractContextManager[Path]: ...


__all__ = [
    "ArchiveEntry",
    "ArchiveGateway",
    "BackupComponentGateway",
    "SchemaVersions",
    "ServiceControl",
    "Workspace",
]
