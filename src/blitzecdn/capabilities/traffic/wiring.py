"""Wire traffic accounting to the fleet, database and audit journal."""

from collections.abc import Callable, Sequence

from blitzecdn.capabilities.traffic.adapters.playbooks import TrafficPlaybooks
from blitzecdn.capabilities.traffic.ports import TrafficStore
from blitzecdn.capabilities.traffic.service.collection import TrafficService
from blitzecdn.capabilities.traffic.settings import TrafficConfig
from blitzecdn.core.features import FeatureOperations, ScheduledJob
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.clock import Clock
from blitzecdn.core.ports.operations import EventRecorder, PlaybookRunner
from blitzecdn.core.runtime.resources import distribution_version

__version__ = distribution_version(__name__)


def build_traffic_service(
    *,
    store: TrafficStore,
    fleet: PlaybookRunner,
    events: EventRecorder,
    uow: UnitOfWork,
    config: TrafficConfig,
    edge_names: Callable[[], Sequence[str]],
    clock: Clock,
) -> TrafficService:
    return TrafficService(
        store=store,
        runner=TrafficPlaybooks(fleet),
        events=events,
        uow=uow,
        config=config,
        edge_names=edge_names,
        clock=clock,
    )


def traffic_operations(
    *,
    traffic: TrafficService,
    config: TrafficConfig,
    has_edges: Callable[[], bool],
    deployment_timeout_seconds: int,
) -> FeatureOperations:
    """A single-flight recurring collection using the durable worker queue."""

    def collect(operator: str) -> None:
        if has_edges():
            traffic.collect_scheduled(operator, max_passes=config.collection_max_passes)

    def prune(operator: str) -> None:
        traffic.prune(operator, retention_days=config.retention_days)

    return FeatureOperations(
        feature="traffic",
        scheduled_jobs=(
            ScheduledJob(
                feature="traffic",
                name="prune-traffic",
                interval_seconds=3600 if config.retention_days else 0,
                run=prune,
                lease_seconds=300,
            ),
            ScheduledJob(
                feature="traffic",
                name="collect-traffic",
                interval_seconds=config.collection_interval_seconds,
                run=collect,
                lease_seconds=max(
                    config.collection_interval_seconds * 2,
                    config.collection_max_passes * deployment_timeout_seconds + 60,
                ),
            ),
        ),
    )
