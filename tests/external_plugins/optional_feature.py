"""A built-in feature module whose feature declares itself optional."""

from blitzecdn.core.plugins import Feature, PluginMetadata

FEATURES = (
    Feature(metadata=PluginMetadata(name="optional", version="1.0", api_version=2)),
)
