"""Backup and restore, registered from a separately installable distribution.

This is what an optional capability looks like. Nothing in ``blitzecdn``
imports this module: the ``blitzecdn.plugins`` entry point in this package's
metadata is the whole of how it is found, so ``uv remove blitzecdn-backup``
removes the capability and ``uv add`` puts it back, with no line of core edited
either way.

``required=False`` is the failure policy that goes with that. A built-in is
required by definition — a control plane without ``sites`` is not degraded but
wrong — whereas a broken optional package is reported by name and skipped, and
the node still serves. ``provides`` is the other half: it is what a
configuration means when it says it depends on this capability, and it is how
the core answers "is backup available here?" without naming this package.

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
        required=False,
        provides=frozenset({"backup"}),
        summary="Archive and restore the control plane's own state.",
    )


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (CliCommandGroup(name="backup", app=cli.backup_app),)
