"""This package's own composition root.

``blitzecdn.composition`` builds the control plane's required services and knows
nothing about what is installed beside it, so an optional distribution wires
itself out of the public contracts core publishes: ``platform.settings``,
``platform.sites`` (the read side of the site model), ``platform.events`` (the
domain-event recorder) and ``platform.fleet`` (run a named play across the
edges). None of those is cache-shaped, and none of them is a repository.

Built once, at registration, from the finished control plane — the same
adapters → services → plugins → contributions order the built-ins follow, one
level out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.core.runtime.resources import distribution_version
from blitzecdn_cache.adapters import CachePlaybooks
from blitzecdn_cache.service import CacheService

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.composition import ControlPlane

#: This distribution's version, asked of the environment rather than
#: written down here: it is what ``PluginMetadata.version`` reports and
#: what ``blitzecdn plugins`` shows an operator, so the one number that
#: must not drift from ``pyproject.toml`` is not copied out of it.
__version__ = distribution_version(__name__)


def build_cache_service(platform: ControlPlane) -> CacheService:
    """Wire the cache capability from what the control plane publishes."""
    return CacheService(
        # Read to decide which hostnames may be purged, and never written:
        # purging is not a change to desired state and must not be able to
        # become one.
        sites=platform.sites,
        events=platform.events,
        runner=CachePlaybooks(platform.fleet),
    )


__all__ = ["__version__", "build_cache_service"]
