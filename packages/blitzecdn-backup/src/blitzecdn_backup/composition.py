"""This package's own composition root.

An optional distribution wires itself. ``blitzecdn.bootstrap`` builds the
control plane's required services and knows nothing about what is installed
beside it, so a package that needs a service builds it here, out of the public
core contracts it is allowed to name: :class:`~blitzecdn.core.config.Settings`,
the filesystem helpers, and the schema identity core keeps for its own database.

Backup is the case that makes the shape obvious. It has to work on a host where
the control plane *cannot* start — a fresh install with an empty database, or
one whose configuration was lost with the disk — so opening a repository to
restore a database would create and migrate the very file about to be replaced.
It therefore takes no repository, holds no port onto another feature, and is
buildable from ``Settings`` alone.
"""

from __future__ import annotations

from blitzecdn.core.config import Settings
from blitzecdn.core.schema import DatabaseSchema
from blitzecdn_backup.adapters import (
    AcmeComponent,
    ComposeRestoreGuard,
    ConfigComponent,
    DatabaseComponent,
    TarArchive,
    TemporaryWorkspace,
    TlsComponent,
)
from blitzecdn_backup.service import BackupPolicy, BackupService

#: The archive format's own version, and this distribution's. Deliberately not
#: the core version: an optional package is released on its own cadence and its
#: metadata should say which *package* is installed.
__version__ = "3.0.0"


def build_backup_service(settings: Settings) -> BackupService:
    """Wire backup and restore, which need no repository and open none."""
    return BackupService(
        policy=BackupPolicy(backup_dir=settings.backup_dir, version=__version__),
        components=(
            DatabaseComponent(settings),
            TlsComponent(settings),
            AcmeComponent(settings),
            ConfigComponent(settings),
        ),
        archive=TarArchive(),
        schema=DatabaseSchema(settings),
        services=ComposeRestoreGuard(),
        # Staged under the state directory rather than /tmp: a staged backup is
        # a complete copy of the database and every private key.
        workspace=TemporaryWorkspace(settings.state_dir / "backup-work"),
    )


__all__ = ["__version__", "build_backup_service"]
