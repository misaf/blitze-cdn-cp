"""Taking an archive, and putting one back.

`archive.py` is both directions and the retention policy between them, because
they are one decision maker: what a restore accepts is exactly what a backup
was allowed to write.
"""

from blitzecdn_backup.service.archive import BackupPolicy, BackupService

__all__ = ["BackupPolicy", "BackupService"]
