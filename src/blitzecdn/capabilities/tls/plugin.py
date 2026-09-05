"""Register the stable TLS policy capability.

One registration for one capability, replacing the two that used to sit beside
each other. The contributions themselves did not merge — a router is still a
router and a job is still a job — but they are declared in one place now, which
is what makes "who owns SslMode" answerable.

The desired-state contribution is the interesting one. A site model can say
which mode a host is in, but only this controller knows the fingerprinted file
the material is actually stored under, so the two TLS paths projected from the
site model are *overridden* here rather than merged beside them. Saying so in
``overrides`` is what makes the merge order-independent: ``sites`` and ``tls``
can register in either order and the edge converges identically.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.core.plugins import PluginMetadata, hookimpl


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="tls",
        version=__version__,
        api_version=1,
        required=True,
        summary="Stable edge-encryption policy and TLS modes.",
    )
