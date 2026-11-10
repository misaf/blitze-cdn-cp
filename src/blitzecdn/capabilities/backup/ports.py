"""What backing up and restoring needs, split by what could be replaced."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from blitzecdn.capabilities.backup.domain import BackupComponent, BackupRecord


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

    def write(self, source: Path, destination: Path) -> None:
        """Pack ``source`` into a new archive at ``destination``, never overwriting."""
        ...

    def entries(self, archive: Path) -> Sequence[ArchiveEntry]:
        """List an archive's members without extracting any of them."""
        ...

    def read_member(self, archive: Path, name: str) -> bytes:
        """One member's bytes, without extracting the archive.

        Raises ``ConfigurationError`` for a member that is missing, not a regular
        file or implausibly large.
        """
        ...

    def extract(self, archive: Path, destination: Path) -> None:
        """Unpack an archive whose :meth:`entries` passed the safety rules.

        Every member is checked again as it is written, so nothing lands outside
        ``destination``.
        """
        ...


class BackupComponentGateway(Protocol):
    """One component's storage; every ``validate`` precedes any ``restore``."""

    @property
    def component(self) -> BackupComponent: ...

    @property
    def requires_offline(self) -> bool:
        """Whether restoring this needs the control-plane services stopped."""
        ...

    def present(self) -> bool:
        """Whether this component exists on this controller, to be backed up."""
        ...

    def export(self, staging: Path) -> None:
        """Write a consistent copy of the live component into ``staging``."""
        ...

    def validate(self, staging: Path) -> None:
        """Check the copy in ``staging`` is whole and usable, changing nothing live.

        Raises ``ConfigurationError`` naming what is wrong.
        """
        ...

    def restore(self, staging: Path) -> None:
        """Replace the live component with the copy in ``staging``.

        Only ever called after :meth:`validate` accepted every component.
        """
        ...

    def recovery_paths(self) -> Sequence[Path]:
        """The live paths a restore replaces, for protection before it starts."""
        ...


class BackupHistory(Protocol):
    """The last full backup this controller completed, readable without a database.

    Kept apart from the database on purpose: a restore is wanted when the
    database is the thing that broke, and monitoring should still say when the
    last good backup was.
    """

    def record(self, entry: BackupRecord) -> None:
        """Make ``entry`` the last completed backup, written atomically."""
        ...

    def latest(self) -> BackupRecord | None:
        """The last completed backup, or ``None`` when there is none to trust.

        An unreadable record reads as no backup rather than raising, so monitoring
        alarms instead of failing.
        """
        ...


class RestoreRecovery(Protocol):
    """Preserve selected live paths and recover them if application fails."""

    def protect(self, paths: Sequence[Path]) -> AbstractContextManager[None]:
        """Capture ``paths``, and put them back if the ``with`` block raises.

        Absent paths are captured as absent, so a failed restore removes what it
        created.
        """
        ...


class SchemaVersions(Protocol):
    """Schema identity and preparation of a database before publication."""

    def current(self) -> str | None:
        """The revision stamped on the live database, or ``None`` if there is none."""
        ...

    def of(self, path: Path) -> str | None:
        """The revision stamped on the database at ``path``; never migrates it."""
        ...

    def known(self, revision: str) -> bool:
        """Whether this build has a migration for ``revision``."""
        ...

    def migrate(self, path: Path) -> None:
        """Upgrade and verify a staged database without touching live state."""
        ...


class ServiceControl(Protocol):
    """Stop the control-plane services for a block; the host owns the restarts."""

    def stopped(self) -> AbstractContextManager[None]:
        """A block during which the control-plane services are not running.

        Raises ``ExecutionError`` when this process cannot be sure they are, such
        as a restore run inside the container rather than through the host.
        """
        ...


class Workspace(Protocol):
    """A private directory that is removed however the caller leaves it."""

    def scratch(self, prefix: str) -> AbstractContextManager[Path]:
        """A new private directory named from ``prefix``, removed on exit."""
        ...


__all__ = [
    "ArchiveEntry",
    "ArchiveGateway",
    "BackupComponentGateway",
    "BackupHistory",
    "RestoreRecovery",
    "SchemaVersions",
    "ServiceControl",
    "Workspace",
]
