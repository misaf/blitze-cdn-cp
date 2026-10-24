"""Declare the baseline HTTP capability and the fleet's listener stance.

HTTP/1.1 and HTTP/2 are invariants of the managed edge: every edge serves
them, nothing turns them on, and no distribution has to be installed for them
to work. That is the whole of what this feature owns; its fleet
stance is rendered by :mod:`blitzecdn.capabilities.http.runtime`.

The two QUIC variables are contributed here at their *baseline* — no listener,
no owner — rather than left out. They are `required: true` in the
``blitzecdn_edge`` and ``blitzecdn_nginx`` argument specs, and the desired-state
document an operator reads should say what the fleet's listener stance is in
every installation rather than only in the ones that happen to have HTTP/3
attached. ``blitzecdn-http3`` declares both in its ``overrides`` and replaces
them when it is installed, which is the same mechanism ``certificates`` uses
for the certificate paths ``dns`` projects.

So the document has one shape whichever distributions are present, and the
difference between "HTTP/3 is not installed" and "no site asked for HTTP/3" is
invisible to the edge — correctly, because the edge does the same thing in
both cases. The difference is made visible where it belongs: a site that asks
for HTTP/3 without the capability installed is refused by name at validation,
before any of this is rendered.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.core.plugins import Feature, PluginMetadata

FEATURES = (
    Feature(
        metadata=PluginMetadata(
            name="http",
            version=__version__,
            api_version=2,
            required=True,
            summary="Baseline visitor HTTP/1.1 and HTTP/2, and the listener contract.",
        ),
    ),
)
