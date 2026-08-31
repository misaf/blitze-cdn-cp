"""Backup and restore, which deliberately contribute no HTTP surface.

Restoring replaces the database this process is reading and stops the services
around it, so it is a command an operator runs on the host and not a request the
control plane can serve — a route that worked would be a route that killed the
worker answering it.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn import __version__
from blitzecdn.core.plugins import CliCommandGroup, PluginMetadata, hookimpl
from blitzecdn.features.backup import cli


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="backup",
        version=__version__,
        required=True,
        summary="Archive and restore the control plane's own state.",
    )


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (CliCommandGroup(name="backup", app=cli.backup_app),)
