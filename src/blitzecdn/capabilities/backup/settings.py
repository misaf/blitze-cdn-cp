"""This capability's own configuration, resolved without a control plane.

Backup's commands open no repository — a broken database is when a restore is
wanted — so this resolves its one contribution with
:func:`~blitzecdn.core.features.resolve_feature_configuration`; cross-feature
checks still run at control-plane startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from blitzecdn.core.config import Settings
from blitzecdn.core.features import (
    CapabilitySetting,
    ConfigurationContribution,
    resolve_feature_configuration,
)

__all__ = ["CONFIGURATION", "BackupConfig"]

BACKUP_DIR = "BLITZE_BACKUP_DIR"

#: Relative, so under the state directory in a checkout; an installed
#: controller is configured to ``/var/backups/blitzecdn``, outside what an
#: uninstall removes.
CONFIGURATION = ConfigurationContribution(
    feature="backup",
    settings=(
        CapabilitySetting(
            name=BACKUP_DIR,
            default=Path("backups"),
            # Not portable: it describes the dead host, not an operator decision.
            portable=False,
            summary=(
                "Where archives are written. A relative path is taken as under "
                "the controller's state directory."
            ),
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class BackupConfig:
    """What this capability needs from the controller's configuration."""

    backup_dir: Path

    @classmethod
    def from_settings(cls, settings: Settings) -> BackupConfig:
        """Resolve this package's own configuration from a bare ``Settings``."""
        config = resolve_feature_configuration(
            CONFIGURATION,
            settings.capability_environment,
            settings.capability_config_file,
            settings.state_dir,
        )
        return cls(backup_dir=config.path(BACKUP_DIR))
