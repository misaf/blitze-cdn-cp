"""Backup and restore, registered from a separately installable distribution.

This is what an optional capability looks like. Nothing in ``blitzecdn``
imports this module: the ``blitzecdn.plugins`` entry point in this package's
metadata is the whole of how it is found, so ``uv remove blitzecdn-backup``
removes the capability and ``uv add`` puts it back, with no line of core edited
either way.

A built-in is required by definition — a control plane without ``sites`` is not
degraded but wrong — whereas an external plugin is optional unless it explicitly
chooses a stricter failure policy. ``PluginMetadata`` also treats the plugin's
own name as a capability automatically, so this package only needs to declare
additional capability tokens when it actually provides something beyond
``backup``.

Deliberately no HTTP surface. Restoring replaces the database this process is
reading and stops the services around it, so it is a command an operator runs
on the host and not a request the control plane can serve — a route that worked
would be a route that killed the worker answering it.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn.core.plugins import CliCommandGroup, PluginMetadata, hookimpl
from blitzecdn_backup import cli
from blitzecdn_backup.composition import __version__


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="backup",
        version=__version__,
        summary="Archive and restore the control plane's own state.",
    )


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (CliCommandGroup(name="backup", app=cli.backup_app),)
