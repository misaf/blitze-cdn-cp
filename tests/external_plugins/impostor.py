"""An external plugin claiming a name a built-in capability already answers to."""

from __future__ import annotations

from blitzecdn.core.plugins import PluginMetadata, hookimpl


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(name="dns", version="0.1", api_version=1)
