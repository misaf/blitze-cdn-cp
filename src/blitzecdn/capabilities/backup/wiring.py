"""Compose the application's backup services from their declared collaborators.

Configuration, ports and concrete adapters meet here. Services and domain
values do not discover features or construct the control plane.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from blitzecdn.capabilities.backup.adapters import (
    AcmeComponent,
    ComposeRestoreGuard,
    ConfigComponent,
    DatabaseComponent,
    TarArchive,
    TemporaryWorkspace,
    TlsComponent,
)
from blitzecdn.capabilities.backup.adapters.recovery import RestoreJournal
from blitzecdn.capabilities.backup.ports import ServiceControl
from blitzecdn.capabilities.backup.service import BackupPolicy, BackupService
from blitzecdn.capabilities.backup.settings import BackupConfig
from blitzecdn.core.config import Settings
from blitzecdn.core.features import ConfigurationContribution
from blitzecdn.core.persistence.schema import DatabaseSchema
from blitzecdn.core.runtime.resources import distribution_version

#: This distribution's version, asked of the environment rather than
#: written down here: it is what ``FeatureMetadata.version`` reports and
#: what ``blitzecdn plugins`` shows an operator, so the one number that
#: must not drift from ``pyproject.toml`` is not copied out of it.
__version__ = distribution_version(__name__)


def build_restore_journal(settings: Settings) -> RestoreJournal:
    """The checkpoint shared by application restore and host publication."""
    return RestoreJournal(
        settings.state_dir / "restore-recovery",
        defer_commit=os.environ.get("COMPOSE_RESTORE_OFFLINE") == "1",
    )


def build_restore_guard() -> ServiceControl:
    return ComposeRestoreGuard()


def build_backup_service(
    settings: Settings, *, configuration: Sequence[ConfigurationContribution]
) -> BackupService:
    """Wire backup and restore, which need no repository and open none."""
    machine_specific = frozenset(
        setting.name.removeprefix("BLITZE_").lower()
        for contribution in configuration
        for setting in contribution.settings
        if not setting.portable
    )
    return BackupService(
        policy=BackupPolicy(
            backup_dir=BackupConfig.from_settings(settings).backup_dir,
            version=__version__,
        ),
        components=(
            DatabaseComponent(settings),
            TlsComponent(settings),
            AcmeComponent(settings),
            ConfigComponent(settings, machine_specific),
        ),
        archive=TarArchive(),
        schema=DatabaseSchema(settings),
        services=build_restore_guard(),
        # Staged under the state directory rather than /tmp: a staged backup is
        # a complete copy of the database and every private key.
        workspace=TemporaryWorkspace(settings.state_dir / "backup-work"),
        recovery=build_restore_journal(settings),
    )


__all__ = ["__version__", "build_backup_service"]
