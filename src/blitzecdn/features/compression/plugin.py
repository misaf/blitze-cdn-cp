"""Register the compression capability.

Metadata only, and deliberately so. Compression has policy — which encodings an
edge may produce for a site — but no fleet-derived state, no operation an
operator invokes, and no failure a deployment check could see that the site's
own validation does not already refuse. Contributing an empty router or a hook
that returns nothing to satisfy a shape would be the "giant universal feature
base class" this plugin system exists to avoid.

What it does buy: the capability is registered, so ``blitzecdn plugins`` lists
it, a failure is attributed to it by name, and the ownership question — where
does Brotli live? — has one answer that a test can check.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.core.plugins import PluginMetadata, hookimpl


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="compression",
        version=__version__,
        required=True,
        summary="Which encodings a managed edge may produce: gzip and Brotli.",
    )
