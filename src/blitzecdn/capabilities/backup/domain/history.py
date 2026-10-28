"""What the controller remembers about the last backup it completed."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.backup.domain.manifest import BackupComponent


class BackupRecord(BaseModel):
    """One full backup that finished, as monitoring needs to see it.

    Only a full backup counts: a ``--only`` archive cannot rebuild a controller,
    so an age measured from one would report a recovery point that does not
    exist.
    """

    model_config = ConfigDict(frozen=True)

    completed_at: datetime
    #: The archive's file name; where it lives is the backup directory's business.
    archive: str
    size_bytes: int
    components: tuple[BackupComponent, ...]
