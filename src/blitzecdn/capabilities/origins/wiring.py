"""Compose the origins services from their declared collaborators."""

from __future__ import annotations

from dishka import Provider, Scope, provide

from blitzecdn.capabilities.origins.adapters import OriginCheckPlaybook
from blitzecdn.capabilities.origins.ports import OriginProbe, SiteReader
from blitzecdn.capabilities.origins.service.checks import OriginCheckService
from blitzecdn.capabilities.origins.settings import CONFIGURATION, OriginsConfig
from blitzecdn.core.features.resolution import ResolvedCapabilityEnvironment
from blitzecdn.core.ports.clock import Clock
from blitzecdn.core.ports.operations import EventRecorder, PlaybookRunner


def build_origin_check_service(
    *,
    sites: SiteReader,
    events: EventRecorder,
    fleet: PlaybookRunner,
    origin_probe: OriginProbe,
    clock: Clock,
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


__all__ = ["OriginsProvider", "build_origin_check_service"]


class OriginsProvider(Provider):
    """The origin-check configuration and service."""

    scope = Scope.APP

    @provide
    def config(self, capability_config: ResolvedCapabilityEnvironment) -> OriginsConfig:
        return capability_config.for_feature(CONFIGURATION.feature).as_model(
            OriginsConfig
        )

    @provide
    def service(
        self,
        sites: SiteReader,
        events: EventRecorder,
        fleet: PlaybookRunner,
        origin_probe: OriginProbe,
        clock: Clock,
    ) -> OriginCheckService:
        return build_origin_check_service(
            sites=sites,
            events=events,
            fleet=fleet,
            origin_probe=origin_probe,
            clock=clock,
        )
