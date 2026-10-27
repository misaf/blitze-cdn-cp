"""Collect outside transactions; commit each edge's increments with its progress."""

import logging

from pydantic import ValidationError

from blitzecdn.capabilities.traffic.domain import (
    CollectedUsage,
    EdgeCollection,
    TrafficUsage,
    UsageQuery,
)
from blitzecdn.capabilities.traffic.ports import TrafficRunner, TrafficStore
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.domain.runs import HostRun
from blitzecdn.core.domain.validation import validate_edge_limit
from blitzecdn.core.exceptions import ConflictError, ExecutionError
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder

_LOGGER = logging.getLogger(__name__)


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

    def prune(self, operator: str, *, retention_days: int) -> None:
        if retention_days == 0:
            return
        with self.uow.transaction():
            result = self.store.prune(retention_days)
            self.events.record(
                domain_event(
                    operator,
                    "traffic.pruned",
                    "traffic",
                    "history",
                    result.model_dump(mode="json"),
                )
            )
        _LOGGER.info(
            "traffic retention pass finished", extra=result.model_dump(mode="json")
        )
        if result.more:
            _LOGGER.warning("expired traffic remains until the next retention pass")

    def usage(self, query: UsageQuery) -> PageResult[TrafficUsage]:
        return self.store.usage(query)

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

    def collect_scheduled(self, operator: str, *, max_passes: int) -> None:
        """Drain bounded batches; stop on failures, no progress, or the pass cap."""
        latest: dict[str, EdgeCollection] = {}
        host_limit = None
        for pass_number in range(1, max_passes + 1):
            results = self.collect(operator, host_limit=host_limit)
            pending: list[str] = []
            for result in results:
                latest[result.edge] = result
                _LOGGER.info(
                    "traffic collection pass finished",
                    extra={"pass_number": pass_number, **result.model_dump()},
                )
                if result.more and result.requests and not result.errors:
                    pending.append(result.edge)
            if not pending or pass_number == max_passes:
                break
            host_limit = ",".join(sorted(pending))
            try:
                validate_edge_limit(host_limit)
            except ValueError:
                # The shared fleet primitive caps target lists. Defer rather
                # than widening a follow-up to every edge or exceeding its bound.
                break
        failed = sorted(edge for edge, result in latest.items() if result.errors)
        if failed:
            raise ExecutionError(
                "traffic collection incomplete on: "
                + ", ".join(failed)
                + "; inspect traffic collection worker logs for details"
            )
        remaining = sorted(edge for edge, result in latest.items() if result.more)
        if remaining:
            _LOGGER.warning(
                "traffic remains pending until the next scheduled collection",
                extra={"edges": remaining, "max_passes": max_passes},
            )

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
