"""Register the required maintenance job executor capability."""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.core.plugins import PluginMetadata, hookimpl


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="maintenance",
        version=__version__,
        required=True,
        summary="Execute and converge scheduled capability jobs.",
    )
