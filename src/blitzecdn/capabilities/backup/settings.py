"""This capability's own configuration, resolved without a control plane.

Backup's commands open no repository — a broken database is when a restore is
wanted — so this resolves its one contribution with
:func:`~blitzecdn.core.features.resolve_feature_configuration`; cross-feature
checks still run at control-plane startup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import Field

from blitzecdn.core.config import Settings
from blitzecdn.core.features import (
    ConfigurationModel,
    resolve_feature_configuration,
)

__all__ = ["CONFIGURATION", "BackupConfig"]

BACKUP_DIR: Final = "BLITZE_BACKUP_DIR"
BACKUP_RETAIN: Final = "BLITZE_BACKUP_RETAIN_COUNT"


#: Relative, so under the state directory in a checkout; an installed
#: controller is configured to ``/var/backups/blitzecdn``, outside what an
#: uninstall removes.
class BackupConfig(ConfigurationModel):
    """What this capability needs from the controller's configuration."""

    backup_dir: Path = Field(
        default=Path("backups"),
        alias=BACKUP_DIR,
        json_schema_extra={"portable": False},
        description=(
            "Where archives are written. A relative path is taken as under "
            "the controller's state directory."
        ),
    )
    retain_count: int = Field(
        default=7,
        ge=0,
        le=1000,
        alias=BACKUP_RETAIN,
        description=(
            "Newest archives 'backup prune' keeps in the backup directory, "
            "counting only names 'backup create' writes there. 0 keeps every "
            "archive."
        ),
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> BackupConfig:
        """Resolve this package's own configuration from a bare ``Settings``."""
        config = resolve_feature_configuration(
            CONFIGURATION,
            settings.capability_environment,
            settings.capability_config_file,
            settings.state_dir,
        )
        return config.as_model(cls)


CONFIGURATION = BackupConfig.configuration("backup")
