"""An external plugin written against a hook contract this control plane predates."""

from __future__ import annotations

from blitzecdn.core.plugins import PluginMetadata, hookimpl


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(name="from-the-future", version="9.0", api_version=99)
