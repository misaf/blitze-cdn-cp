"""This capability's own configuration, read from what the controller was given.

One setting, and the interesting part is where it is read from. Backup's
commands are built from ``Settings`` alone and open no repository — that is
deliberate, since a controller whose database will not open is exactly when a
restore is wanted — so there is no ``ControlPlane`` on this path and no
``platform.capability_config`` to ask.

:func:`~blitzecdn.core.plugins.resolve_plugin_configuration` is core's answer
to that: the same resolution the composition root performs, for one
contribution, against the staged configuration ``Settings`` already carries.
The cross-plugin half — a name two capabilities claim, a name nobody claims —
is a question about the whole installation and is still answered once, at
startup, by the control plane. This resolves a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from blitzecdn.core.config import Settings
from blitzecdn.core.plugins import (
    CapabilitySetting,
    ConfigurationContribution,
    resolve_plugin_configuration,
)

__all__ = ["CONFIGURATION", "BackupConfig"]

BACKUP_DIR = "BLITZE_BACKUP_DIR"

#: Under the state directory in a checkout, and repointed at
#: ``/var/backups/blitzecdn`` by the managed configuration a real installation
#: gets. Both are right for where they are: a developer should not need a
#: privileged directory to take a backup, and an installed controller should
#: not keep its backups inside the directory an uninstall removes.
#:
#: Declared relative, which is what says "under this controller's state" without
#: this package having to know where that is. Core resolves it.
CONFIGURATION = ConfigurationContribution(
    plugin="backup",
    settings=(
        CapabilitySetting(
            name=BACKUP_DIR,
            default=Path("backups"),
            # Not portable. An archive carries the operator's decisions onto a
            # rebuilt controller, and where *the dead host* kept its backups is
            # not one of them: restoring it would point the new controller's
            # archives at a path belonging to the machine it is replacing.
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
        config = resolve_plugin_configuration(
            CONFIGURATION,
            settings.capability_environment,
            settings.capability_config_file,
            settings.state_dir,
        )
        return cls(backup_dir=config.path(BACKUP_DIR))
