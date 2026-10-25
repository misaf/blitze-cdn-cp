"""Archive creation and recovery for the application.

Shipped with the application. Contracts stay pure; services and concrete
providers are assembled explicitly by composition.
"""

from blitzecdn.capabilities.backup.domain import BackupComponent
from blitzecdn.capabilities.backup.service import BackupService
from blitzecdn.capabilities.backup.wiring import __version__, build_backup_service

__all__ = ["BackupComponent", "BackupService", "__version__", "build_backup_service"]
