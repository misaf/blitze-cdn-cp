"""Wire traffic accounting to the fleet, database and audit journal."""

from blitzecdn.capabilities.traffic.adapters.playbooks import TrafficPlaybooks
from blitzecdn.capabilities.traffic.ports import TrafficStore
from blitzecdn.capabilities.traffic.service.collection import TrafficService
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder, PlaybookRunner
from blitzecdn.core.runtime.resources import distribution_version

__version__ = distribution_version(__name__)


def build_traffic_service(
    *,
    store: TrafficStore,
    fleet: PlaybookRunner,
    events: EventRecorder,
    uow: UnitOfWork,
) -> TrafficService:
    return TrafficService(
        store=store, runner=TrafficPlaybooks(fleet), events=events, uow=uow
    )
