"""Register the application's backup implementation.

All code and resources ship with BlitzeCDN. Registration contributes delivery,
configuration or concrete edge resources; business operations remain services.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn.capabilities.backup import cli
from blitzecdn.capabilities.backup.composition import __version__
from blitzecdn.capabilities.backup.config import CONFIGURATION
from blitzecdn.core.plugins import (
    CliCommandGroup,
    ConfigurationContribution,
    PluginMetadata,
    hookimpl,
)


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="backup",
        version=__version__,
        api_version=1,
        required=True,
        provides=frozenset({"backup"}),
        summary="Archive and restore the control plane's own state.",
    )


@hookimpl
def blitzecdn_capability_configuration() -> Sequence[ConfigurationContribution]:
    """Claim where this capability writes its archives.

    The same declaration this package resolves for itself in
    :mod:`blitzecdn.capabilities.backup.config` when it is reached from the command line
    with no control plane. Claimed here as well, and it has to be: claiming is
    what makes `BLITZE_BACKUP_DIR` a name the installation recognises rather
    than one the startup check rejects as belonging to nobody.
    """
    return (CONFIGURATION,)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (CliCommandGroup(plugin="backup", name="backup", app=cli.backup_app),)
