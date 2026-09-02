"""This package's own composition root.

``blitzecdn.bootstrap`` builds the control plane's required services and knows
nothing about what is installed beside it, so an optional distribution wires
itself out of the public contracts core publishes: ``platform.sites`` (the read
side of the site model), ``platform.events`` (the domain-event recorder),
``platform.origin_probe`` (how to describe an origin to whoever will connect to
it) and ``platform.fleet`` (run a named play across the edges). None of those is
origin-check-shaped, and none of them is a repository.

Built once, at registration, from the finished control plane — the same
adapters → services → plugins → contributions order the built-ins follow, one
level out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn_origins.adapters import OriginCheckPlaybook
from blitzecdn_origins.service import OriginCheckService

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane

#: This distribution's version, released on its own cadence.
__version__ = "3.0.0"


def build_origin_check_service(platform: ControlPlane) -> OriginCheckService:
    """Wire the origin-check capability from what the control plane publishes."""
    return OriginCheckService(
        # Read to decide which origins to probe, and never written: a check is
        # not a change to desired state and must not be able to become one.
        sites=platform.sites,
        events=platform.events,
        runner=OriginCheckPlaybook(platform.fleet),
        origin_probe=platform.origin_probe,
    )


__all__ = ["__version__", "build_origin_check_service"]
