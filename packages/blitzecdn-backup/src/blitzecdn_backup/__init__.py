"""Backup and restore: an optional BlitzeCDN capability, installed separately.

The public face of the distribution. ``blitzecdn`` never imports it — the
control plane finds this package through its ``blitzecdn.plugins`` entry point
or not at all.
"""

from blitzecdn_backup.composition import __version__, build_backup_service
from blitzecdn_backup.domain import BackupComponent
from blitzecdn_backup.service import BackupService

__all__ = ["BackupComponent", "BackupService", "__version__", "build_backup_service"]
