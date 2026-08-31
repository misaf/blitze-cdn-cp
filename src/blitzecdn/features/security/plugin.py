"""Register the security capability.

Metadata only. The firewall rules and the Under Attack Mode switch are policy
on a site, projected into the edge document by ``sites`` like every other
setting, so this capability contributes no state of its own and invokes no
operation an operator asks for.

What it does buy: the capability is registered, so ``blitzecdn plugins`` lists
it, a failure is attributed to it by name, and the ownership question — where
does Under Attack Mode live? — has one answer that a test can check.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.core.plugins import PluginMetadata, hookimpl


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="security",
        version=__version__,
        required=True,
        summary="Per-site request filtering and Under Attack Mode.",
    )
