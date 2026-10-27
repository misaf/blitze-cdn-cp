"""Collect outside transactions; commit each edge's increments with its progress."""

from pydantic import ValidationError

from blitzecdn.capabilities.traffic.domain import CollectedUsage, EdgeCollection
from blitzecdn.capabilities.traffic.ports import TrafficRunner, TrafficStore
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.domain.runs import HostRun
from blitzecdn.core.exceptions import ConflictError, ExecutionError
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder


class TrafficService:
    def __init__(
        self,
        *,
        store: TrafficStore,
        runner: TrafficRunner,
        events: EventRecorder,
        uow: UnitOfWork,
    ) -> None:
        self.store = store
        self.runner = runner
        self.events = events
        self.uow = uow

    def collect(
        self, operator: str, *, host_limit: str | None = None
    ) -> tuple[EdgeCollection, ...]:
        run = self.runner.collect(self.store.checkpoints(), host_limit=host_limit)
        if not run.hosts:
            raise ExecutionError(run.unreported("traffic collection"))
        results = [self._edge(host, operator) for host in run.hosts]
        reported = {host.host for host in run.hosts}
        results.extend(
            EdgeCollection(edge=edge, errors=("edge returned no traffic report",))
            for edge in run.targeted
            if edge not in reported
        )
        return tuple(results)

    def _edge(self, host: HostRun, operator: str) -> EdgeCollection:
        if not host.succeeded or host.report is None:
            return EdgeCollection(
                edge=host.host,
                errors=("edge unreachable, failed, or returned no traffic report",),
            )
        try:
            collection = CollectedUsage.model_validate(host.report)
            with self.uow.transaction():
                result = self.store.apply(host.host, collection)
                self.events.record(
                    domain_event(
                        operator,
                        "traffic.collected",
                        "edge",
                        host.host,
                        result.model_dump(mode="json"),
                    )
                )
                return result
        except ValidationError:
            return EdgeCollection(
                edge=host.host, errors=("edge returned an invalid traffic report",)
            )
        except ConflictError as exc:
            return EdgeCollection(edge=host.host, errors=(str(exc),), more=True)
