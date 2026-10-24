"""Compose the application's origins services from their declared collaborators.

Configuration, ports and concrete adapters meet here. Services and domain
values do not discover installed packages or construct the control plane.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.capabilities.origins.adapters import OriginCheckPlaybook
from blitzecdn.capabilities.origins.service.checks import OriginCheckService

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.composition import ControlPlane


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


__all__ = ["build_origin_check_service"]
