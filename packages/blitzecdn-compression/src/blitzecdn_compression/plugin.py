"""Register gzip and Brotli as one detachable compression capability."""

from blitzecdn.core.plugins import PluginMetadata, hookimpl

__version__ = "3.0.0"


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="compression",
        version=__version__,
        summary="Which encodings a managed edge may produce: gzip and Brotli.",
    )
