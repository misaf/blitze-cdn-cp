"""Build the one plugin manager a process has.

One per process, built by the composition root and handed to whoever needs it.
There is deliberately no module-level manager and no ``get_plugin_manager()``:
a global would be reachable from anywhere, which is precisely the property that
turns an extension registry into a service locator. A test builds its own with
whichever plugins the test is about.
"""

from __future__ import annotations

from collections.abc import Sequence

import pluggy

from blitzecdn.core.plugins import hooks
from blitzecdn.core.plugins.discovery import register_builtins, register_external
from blitzecdn.core.plugins.registry import PluginRegistry
from blitzecdn.core.plugins.types import ENTRY_POINT_GROUP, PROJECT_NAME


def build_plugin_manager() -> pluggy.PluginManager:
    """An empty manager that knows the hook specifications and nothing else."""
    manager = pluggy.PluginManager(PROJECT_NAME)
    manager.add_hookspecs(hooks)
    return manager


def load_plugins(
    builtins: Sequence[str],
    *,
    entry_point_group: str | None = ENTRY_POINT_GROUP,
    manager: pluggy.PluginManager | None = None,
) -> PluginRegistry:
    """Discover and register everything, built-in first.

    ``builtins`` is an argument rather than a default read from this package,
    because which capabilities a distribution ships is the composition root's
    answer and not core's. `blitzecdn.bootstrap.load_control_plane_plugins` is
    the call that pairs this mechanism with that roster; everything outside
    core wants that one.

    Built-ins go first so an external plugin that claims a built-in's name
    collides with the built-in rather than displacing it, and so the command
    tree an operator sees begins with the commands this distribution ships.

    ``entry_point_group=None`` skips external discovery entirely. That is what
    the unit suite uses: a test asserting on the built-in capability set should
    not change its answer because a developer happens to have an unrelated
    BlitzeCDN plugin installed in the same virtualenv.
    """
    manager = manager or build_plugin_manager()
    found = register_builtins(manager, builtins)
    external = (
        register_external(manager, group=entry_point_group)
        if entry_point_group is not None
        else None
    )
    return PluginRegistry(
        manager,
        plugins=(*found, *(external.plugins if external else ())),
        rejected=external.rejected if external else (),
    )


__all__ = ["build_plugin_manager", "load_plugins"]
