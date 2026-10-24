"""Compose the application's cache services from their declared collaborators.

Configuration, ports and concrete adapters meet here. Services and domain
values do not discover installed packages or construct the control plane.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.capabilities.cache.adapters import CachePlaybooks
from blitzecdn.capabilities.cache.service import CacheService
from blitzecdn.core.runtime.resources import distribution_version

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
