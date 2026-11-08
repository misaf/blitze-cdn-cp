"""The application's service graph, assembled from the roster.

Each capability's `wiring.py` provider builds its own stores and services;
:mod:`blitzecdn.composition.bindings` says which capability satisfies another's
port. What is left here is binding every feature's behaviour once the services
it closes over exist. Processes are in :mod:`blitzecdn.composition.processes`.
"""

from __future__ import annotations

from dishka import Provider, Scope, provide

from blitzecdn.capabilities.jobs.ports import OperationRuns
from blitzecdn.composition.bindings import PortBindings
from blitzecdn.composition.roster import (
    OperationInputs,
    RenderingInputs,
    bind_operations,
    bind_queued_operations,
    bind_renderings,
    capability_providers,
)
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.features import FeatureRegistry, Operations, Rendering

__all__ = ["ServiceProvider", "service_providers"]


class ServiceProvider(Provider):
    """Every feature's rendering, operations and queued edge operations."""

    scope = Scope.APP

    rendering_inputs = provide(RenderingInputs)
    operation_inputs = provide(OperationInputs)

    @provide
    def rendering(
        self, features: FeatureRegistry, inputs: RenderingInputs
    ) -> Rendering[CdnSite]:
        """What every feature renders into a release, from configuration and
        adapters only."""
        return features.bind_rendering(bind_renderings(inputs))

    @provide
    def operations(
        self, features: FeatureRegistry, inputs: OperationInputs
    ) -> Operations:
        """Contributed jobs close over services, so they bind once every
        service exists."""
        return features.bind_operations(bind_operations(inputs))

    @provide
    def operation_runs(self, inputs: OperationInputs) -> OperationRuns:
        return OperationRuns(bind_queued_operations(inputs))


def service_providers() -> tuple[Provider, ...]:
    """Every capability's provider, the port bindings, and feature binding."""
    return (*capability_providers(), PortBindings(), ServiceProvider())
