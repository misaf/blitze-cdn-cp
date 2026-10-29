"""The last completed backup, kept as one small file in the state directory."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from blitzecdn.capabilities.backup.domain import BackupRecord
from blitzecdn.core.runtime.filesystem import atomic_write_bytes


class BackupStatusFile:
    """A JSON record replaced whole on every full backup.

    In the state directory because every control-plane container mounts it:
    the CLI writes it, and the API reports it without needing the backup
    directory itself.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, entry: BackupRecord) -> None:
        atomic_write_bytes(
            self._path, entry.model_dump_json(indent=2).encode("utf-8") + b"\n"
        )

    def latest(self) -> BackupRecord | None:
        """The recorded backup, or ``None`` when there is none to trust.

        An unreadable record reads as no backup, which is what monitoring
        should alarm on, rather than as an error that takes reporting down.
        """
        try:
            return BackupRecord.model_validate_json(self._path.read_bytes())
        except (OSError, ValidationError):
            return None
