"""Declare the application's backup capability."""

from __future__ import annotations

from blitzecdn.capabilities.backup import cli
from blitzecdn.capabilities.backup.settings import CONFIGURATION
from blitzecdn.capabilities.backup.wiring import __version__
from blitzecdn.core.features import CliCommandGroup, Feature, FeatureMetadata

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="backup",
            version=__version__,
            provides=frozenset({"backup"}),
            summary="Archive and restore the control plane's own state.",
        ),
        cli_commands=(
            CliCommandGroup(feature="backup", name="backup", app=cli.backup_app),
        ),
        # Where this capability writes its archives. The same declaration the
        # backup CLI resolves for itself when it runs with no control plane;
        # claiming it here is what makes `BLITZE_BACKUP_DIR` a name the
        # installation recognises rather than one the startup check rejects.
        configuration=(CONFIGURATION,),
    ),
)
