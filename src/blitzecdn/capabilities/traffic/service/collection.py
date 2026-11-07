"""Collect outside transactions; commit each edge's increments with its progress."""

import logging
from collections.abc import Callable, Sequence
from uuid import uuid4

from pydantic import ValidationError

from blitzecdn.capabilities.traffic.domain import (
    CollectedUsage,
    CollectionStatus,
    EdgeCollection,
    FreshnessState,
    TrafficUsage,
    UsageQuery,
)
from blitzecdn.capabilities.traffic.ports import TrafficRunner, TrafficStore
from blitzecdn.capabilities.traffic.settings import TrafficConfig
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.domain.runs import HostRun
from blitzecdn.core.domain.validation import matches_edge_limit, validate_edge_limit
from blitzecdn.core.exceptions import ConflictError, ExecutionError, NotFoundError
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.clock import Clock, system_clock
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
        config: TrafficConfig | None = None,
        edge_names: Callable[[], Sequence[str]] = lambda: (),
        clock: Clock = system_clock,
    ) -> None:
        self.store = store
        self.runner = runner
        self.events = events
        self.uow = uow
        self.config = config or TrafficConfig()
        self.edge_names = edge_names
        self.clock = clock

    def status(self, edge: str) -> CollectionStatus:
        record = self.store.collection_status(edge)
        registered = edge in self.edge_names()
        if not registered and record is None:
            raise NotFoundError(
                f"edge {edge!r} has no collection history and is not registered"
            )
        now = self.clock()
        interval = self.config.collection_interval_seconds
        threshold = max(60, interval * 2)
        enabled = registered and interval > 0
        stale = (
            record is None
            or record.last_caught_up_at is None
            or (now - record.last_caught_up_at).total_seconds() >= threshold
        )
        if not enabled:
            state = FreshnessState.DISABLED
        elif record is None:
            state = FreshnessState.NEVER_COLLECTED
        elif record.finished_at is None:
            state = (
                FreshnessState.STALE
                if (now - record.started_at).total_seconds() >= threshold
                else FreshnessState.COLLECTING
            )
        elif record.last_error:
            state = FreshnessState.FAILED
        elif record.pending:
            state = FreshnessState.BACKLOGGED
        else:
            state = FreshnessState.STALE if stale else FreshnessState.CURRENT
        return CollectionStatus(
            edge=edge,
            registered=registered,
            collection_enabled=enabled,
            interval_seconds=interval,
            stale_after_seconds=threshold,
            checked_at=now,
            state=state,
            stale=stale,
            last_attempt_started_at=record.started_at if record else None,
            last_attempt_finished_at=record.finished_at if record else None,
            last_success_at=record.last_success_at if record else None,
            last_caught_up_at=record.last_caught_up_at if record else None,
            pending=record.pending if record else None,
            last_error=record.last_error if record else None,
        )

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
        host_limit = validate_edge_limit(host_limit)
        targets = tuple(
            name
            for name in self.edge_names()
            if host_limit is None or matches_edge_limit(name, host_limit)
        )
        attempt_id = uuid4().hex
        started_at = self.clock()
        self.store.start_collection(targets, attempt_id, started_at)
        try:
            run = self.runner.collect(self.store.checkpoints(), host_limit=host_limit)
            if not run.hosts:
                raise ExecutionError(run.unreported("traffic collection"))
        except Exception:
            for edge in targets:
                self.store.finish_collection(
                    EdgeCollection(edge=edge, errors=("fleet collection failed",)),
                    attempt_id,
                )
            raise
        # The runner's inventory snapshot may include edges added after ours.
        reported = {host.host for host in run.hosts}
        unexpected = (reported | set(run.targeted)) - set(targets)
        self.store.start_collection(tuple(unexpected), attempt_id, started_at)
        results = [self._edge(host, operator, attempt_id) for host in run.hosts]
        for edge in sorted((set(targets) | set(run.targeted)) - reported):
            result = EdgeCollection(
                edge=edge, errors=("edge returned no traffic report",)
            )
            self.store.finish_collection(result, attempt_id)
            results.append(result)
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

    def _edge(self, host: HostRun, operator: str, attempt_id: str) -> EdgeCollection:
        if not host.succeeded or host.report is None:
            result = EdgeCollection(
                edge=host.host,
                errors=("edge unreachable, failed, or returned no traffic report",),
            )
            self.store.finish_collection(result, attempt_id)
            return result
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
                self.store.finish_collection(result, attempt_id)
                return result
        except ValidationError:
            result = EdgeCollection(
                edge=host.host, errors=("edge returned an invalid traffic report",)
            )
        except ConflictError as exc:
            result = EdgeCollection(edge=host.host, errors=(str(exc),), more=True)
        except Exception:
            self.store.finish_collection(
                EdgeCollection(edge=host.host, errors=("collection could not commit",)),
                attempt_id,
            )
            raise
        self.store.finish_collection(result, attempt_id)
        return result
