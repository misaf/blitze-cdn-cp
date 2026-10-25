"""Compose the application's origins services from their declared collaborators.

Configuration, ports and concrete adapters meet here. Services and domain
values do not discover installed packages or construct the control plane.
"""

from __future__ import annotations

from blitzecdn.capabilities.origins.adapters import OriginCheckPlaybook
from blitzecdn.capabilities.origins.ports import OriginProbe, SiteReader
from blitzecdn.capabilities.origins.service.checks import OriginCheckService
from blitzecdn.core.ports.clock import Clock, system_clock
from blitzecdn.core.ports.operations import EventRecorder, PlaybookRunner


def build_origin_check_service(
    *,
    sites: SiteReader,
    events: EventRecorder,
    fleet: PlaybookRunner,
    origin_probe: OriginProbe,
    clock: Clock = system_clock,
) -> OriginCheckService:
    """Wire the origin-check capability to the fleet and the derived hosts."""
    return OriginCheckService(
        # Read to decide which origins to probe, and never written: a check is
        # not a change to desired state and must not be able to become one.
        sites=sites,
        events=events,
        runner=OriginCheckPlaybook(fleet),
        origin_probe=origin_probe,
        clock=clock,
    )


__all__ = ["build_origin_check_service"]
