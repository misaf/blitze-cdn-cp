"""Commit increments and compare-and-swap log progress in one transaction."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, delete, func, select, tuple_
from sqlalchemy.orm import Session

from blitzecdn.capabilities.traffic.adapters.tables import (
    TrafficCacheHourRow,
    TrafficCollectionRow,
    TrafficCursorRow,
    TrafficHourRow,
    TrafficPathHourRow,
    TrafficRetentionRow,
    TrafficStatusHourRow,
)
from blitzecdn.capabilities.traffic.domain import (
    STORED_PATHS,
    CacheCounters,
    CacheOutcome,
    CollectedUsage,
    CollectionRecord,
    EdgeCollection,
    HourlyCounters,
    HourlyUsage,
    LogCursor,
    PathCounters,
    RangeQuery,
    RetentionResult,
    SiteHour,
    StatusClass,
    TrafficUsage,
    UsageQuery,
)
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.exceptions import ConflictError
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.persistence.pagination import read_page

type _HourKey = tuple[str, str, datetime]
type _Shares = dict[CacheOutcome, CacheCounters]


class TrafficStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def collection_status(self, edge: str) -> CollectionRecord | None:
        with self._db.session() as session:
            row = session.get(TrafficCollectionRow, edge)
            if row is None:
                return None
            return CollectionRecord(
                edge=row.edge,
                started_at=row.started_at,
                finished_at=row.finished_at,
                last_success_at=row.last_success_at,
                last_caught_up_at=row.last_caught_up_at,
                pending=row.pending,
                last_error=row.last_error,
            )

    def start_collection(
        self, edges: Sequence[str], attempt_id: str, started_at: datetime
    ) -> None:
        with self._db.transaction(), self._db.session() as session:
            for edge in edges:
                row = session.get(TrafficCollectionRow, edge)
                if row is None:
                    session.add(
                        TrafficCollectionRow(
                            edge=edge, attempt_id=attempt_id, started_at=started_at
                        )
                    )
                elif row.started_at <= started_at:
                    row.attempt_id = attempt_id
                    row.started_at = started_at
                    row.finished_at = None
                    row.pending = None
                    row.last_error = None

    def finish_collection(self, result: EdgeCollection, attempt_id: str) -> None:
        with self._db.transaction(), self._db.session() as session:
            row = session.get(TrafficCollectionRow, result.edge)
            if row is None or row.attempt_id != attempt_id:
                return
            row.finished_at = self._db.now()
            row.pending = None if result.errors else result.more
            row.last_error = (
                "Collection failed; inspect worker logs and job status."
                if result.errors
                else None
            )
            if not result.errors:
                row.last_success_at = row.finished_at
                if not result.more:
                    row.last_caught_up_at = row.started_at

    def prune(self, retention_days: int) -> RetentionResult:
        if not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        proposed = (
            self._db.now().astimezone(UTC) - timedelta(days=retention_days)
        ).replace(minute=0, second=0, microsecond=0)
        with self._db.transaction(), self._db.session() as session:
            state = session.get(TrafficRetentionRow, 1)
            if state is None:
                state = TrafficRetentionRow(id=1, cutoff=proposed)
                session.add(state)
            else:
                state.cutoff = max(state.cutoff, proposed)
            session.flush()
            expired = (
                select(
                    TrafficHourRow.edge, TrafficHourRow.hostname, TrafficHourRow.hour
                )
                .where(TrafficHourRow.hour < state.cutoff)
                .order_by(
                    TrafficHourRow.hour, TrafficHourRow.hostname, TrafficHourRow.edge
                )
                .limit(10_000)
            )
            deleted = list(
                session.scalars(
                    delete(TrafficHourRow)
                    .where(
                        tuple_(
                            TrafficHourRow.edge,
                            TrafficHourRow.hostname,
                            TrafficHourRow.hour,
                        ).in_(expired)
                    )
                    .returning(TrafficHourRow.hour),
                    execution_options={"synchronize_session": False},
                )
            )
            more = (
                session.scalar(
                    select(TrafficHourRow.hour)
                    .where(TrafficHourRow.hour < state.cutoff)
                    .limit(1)
                )
                is not None
            )
            return RetentionResult(
                cutoff=state.cutoff, deleted_hours=len(deleted), more=more
            )

    def usage(self, query: UsageQuery) -> PageResult[TrafficUsage]:
        statement = select(TrafficHourRow).where(
            TrafficHourRow.hour >= query.start,
            TrafficHourRow.hour < query.end,
        )
        if query.hostname is not None:
            statement = statement.where(TrafficHourRow.hostname == query.hostname)
        if query.edge is not None:
            statement = statement.where(TrafficHourRow.edge == query.edge)
        statement = statement.order_by(
            TrafficHourRow.hour, TrafficHourRow.hostname, TrafficHourRow.edge
        )
        with self._db.session() as session:
            state = session.get(TrafficRetentionRow, 1)
            if state is not None:
                statement = statement.where(TrafficHourRow.hour >= state.cutoff)
            page = read_page(
                session,
                statement,
                lambda row: row,
                limit=query.limit,
                offset=query.offset,
            )
            outcomes = self._outcomes(
                session, [(row.edge, row.hostname, row.hour) for row in page.items]
            )
            return PageResult(
                items=[
                    TrafficUsage(
                        edge=row.edge, **self._usage(row, outcomes).model_dump()
                    )
                    for row in page.items
                ],
                total=page.total,
            )

    def checkpoints(self) -> dict[str, tuple[LogCursor, ...]]:
        grouped: dict[str, list[LogCursor]] = {}
        with self._db.session() as session:
            for row in session.scalars(
                select(TrafficCursorRow).order_by(
                    TrafficCursorRow.latest_hour,
                    TrafficCursorRow.edge,
                    TrafficCursorRow.stream_id,
                )
            ):
                grouped.setdefault(row.edge, []).append(self._cursor(row))
        return {edge: tuple(cursors) for edge, cursors in grouped.items()}

    def apply(self, edge: str, collection: CollectedUsage) -> EdgeCollection:
        requests = bytes_sent = 0
        with self._db.transaction(), self._db.session() as session:
            state = session.get(TrafficRetentionRow, 1)
            for batch in collection.batches:
                current = session.get(TrafficCursorRow, (edge, batch.end.stream_id))
                if current and current.offset >= batch.end.offset:
                    if (
                        current.offset == batch.end.offset
                        and current.anchor != batch.end.anchor
                    ):
                        raise ConflictError(
                            "traffic stream changed at an already committed offset"
                        )
                    continue
                if (self._cursor(current) if current else None) != batch.start:
                    raise ConflictError(
                        "traffic progress changed; collect again "
                        "from current checkpoints"
                    )
                for bucket in batch.buckets:
                    if state is not None and bucket.hour < state.cutoff:
                        continue
                    self._increment(session, edge, bucket)
                    requests += bucket.requests
                    bytes_sent += bucket.bytes_sent
                latest_hour = max(bucket.hour for bucket in batch.buckets)
                if current is None:
                    current = TrafficCursorRow(
                        edge=edge, latest_hour=latest_hour, **batch.end.model_dump()
                    )
                    session.add(current)
                else:
                    current.offset = batch.end.offset
                    current.anchor = batch.end.anchor
                    current.latest_hour = max(current.latest_hour, latest_hour)
                session.flush()
            if state is not None and collection.missing_streams:
                session.execute(
                    delete(TrafficCursorRow).where(
                        TrafficCursorRow.edge == edge,
                        TrafficCursorRow.stream_id.in_(collection.missing_streams),
                        TrafficCursorRow.latest_hour < state.cutoff,
                    )
                )
            return EdgeCollection(
                edge=edge,
                requests=requests,
                bytes_sent=bytes_sent,
                errors=collection.errors,
                more=collection.more,
            )

    @staticmethod
    def _increment(session: Session, edge: str, bucket: HourlyUsage) -> None:
        key = (edge, bucket.hostname, bucket.hour)
        row = session.get(TrafficHourRow, key)
        if row is None:
            session.add(
                TrafficHourRow(
                    edge=edge,
                    **bucket.model_dump(exclude={"cache", "status", "paths"}),
                )
            )
        else:
            _add(row, bucket, ("requests", "bytes_sent", "body_bytes_sent"))
        session.flush()
        for outcome, counters in bucket.cache.items():
            share = session.get(TrafficCacheHourRow, (*key, outcome.value))
            if share is None:
                session.add(
                    TrafficCacheHourRow(
                        edge=edge,
                        hostname=bucket.hostname,
                        hour=bucket.hour,
                        outcome=outcome.value,
                        **counters.model_dump(),
                    )
                )
            else:
                _add(share, counters, ("requests", "bytes_sent"))
        for status_class, requests in bucket.status.items():
            counted = session.get(TrafficStatusHourRow, (*key, status_class.value))
            if counted is None:
                session.add(
                    TrafficStatusHourRow(
                        edge=edge,
                        hostname=bucket.hostname,
                        hour=bucket.hour,
                        status_class=status_class.value,
                        requests=requests,
                    )
                )
            else:
                _add(counted, {"requests": requests}, ("requests",))
        for item in bucket.paths:
            path = session.get(TrafficPathHourRow, (*key, item.path))
            if path is None:
                session.add(
                    TrafficPathHourRow(
                        edge=edge,
                        hostname=bucket.hostname,
                        hour=bucket.hour,
                        **item.model_dump(),
                    )
                )
            else:
                _add(path, item, ("requests", "bytes_sent"))
        session.flush()
        if bucket.paths:
            TrafficStore._trim_paths(session, key)

    @staticmethod
    def _trim_paths(session: Session, key: _HourKey) -> None:
        """Keep only the hour's best-ranked paths, so storage stays bounded."""
        edge, hostname, hour = key
        ranked = (
            select(TrafficPathHourRow.path)
            .where(
                TrafficPathHourRow.edge == edge,
                TrafficPathHourRow.hostname == hostname,
                TrafficPathHourRow.hour == hour,
            )
            .order_by(TrafficPathHourRow.requests.desc(), TrafficPathHourRow.path)
            .offset(STORED_PATHS)
        )
        session.execute(
            delete(TrafficPathHourRow).where(
                TrafficPathHourRow.edge == edge,
                TrafficPathHourRow.hostname == hostname,
                TrafficPathHourRow.hour == hour,
                TrafficPathHourRow.path.in_(ranked.limit(-1)),
            ),
            execution_options={"synchronize_session": False},
        )

    @staticmethod
    def _outcomes(
        session: Session, keys: Sequence[_HourKey]
    ) -> dict[_HourKey, _Shares]:
        """Each hour row's cache outcomes; one query for a whole page."""
        grouped: dict[_HourKey, _Shares] = {key: {} for key in keys}
        if not keys:
            return grouped
        for share in session.scalars(
            select(TrafficCacheHourRow).where(
                tuple_(
                    TrafficCacheHourRow.edge,
                    TrafficCacheHourRow.hostname,
                    TrafficCacheHourRow.hour,
                ).in_(keys)
            )
        ):
            grouped[(share.edge, share.hostname, share.hour)][
                CacheOutcome(share.outcome)
            ] = CacheCounters(requests=share.requests, bytes_sent=share.bytes_sent)
        return grouped

    @staticmethod
    def _usage(
        row: TrafficHourRow, outcomes: dict[_HourKey, _Shares]
    ) -> HourlyCounters:
        return HourlyCounters(
            hostname=row.hostname,
            hour=row.hour,
            requests=row.requests,
            bytes_sent=row.bytes_sent,
            body_bytes_sent=row.body_bytes_sent,
            cache=outcomes[(row.edge, row.hostname, row.hour)],
        )

    def hourly(
        self, *, edge: str, hostname: str, start: datetime, end: datetime
    ) -> tuple[HourlyCounters, ...]:
        with self._db.session() as session:
            rows = session.scalars(
                select(TrafficHourRow)
                .where(
                    TrafficHourRow.edge == edge,
                    TrafficHourRow.hostname == hostname,
                    TrafficHourRow.hour >= start,
                    TrafficHourRow.hour < end,
                )
                .order_by(TrafficHourRow.hour)
            ).all()
            outcomes = self._outcomes(
                session, [(row.edge, row.hostname, row.hour) for row in rows]
            )
            return tuple(self._usage(row, outcomes) for row in rows)

    def hostnames(self, *, start: datetime, end: datetime) -> frozenset[str]:
        """Every hostname with retained traffic in ``[start, end)``."""
        with self._db.session() as session:
            statement = (
                select(TrafficHourRow.hostname)
                .where(TrafficHourRow.hour >= start, TrafficHourRow.hour < end)
                .distinct()
            )
            statement = self._retained(session, statement, TrafficHourRow.hour)
            return frozenset(session.scalars(statement))

    def site_hours(self, query: RangeQuery) -> tuple[SiteHour, ...]:
        """The hostnames' counters summed over every edge, per non-empty hour."""
        if not query.hostnames:
            return ()
        hostnames = sorted(query.hostnames)
        with self._db.session() as session:
            hours: dict[datetime, dict[str, Any]] = {}
            totals = self._retained(
                session,
                select(
                    TrafficHourRow.hour,
                    func.sum(TrafficHourRow.requests),
                    func.sum(TrafficHourRow.bytes_sent),
                    func.sum(TrafficHourRow.body_bytes_sent),
                )
                .where(
                    TrafficHourRow.hour >= query.start,
                    TrafficHourRow.hour < query.end,
                    TrafficHourRow.hostname.in_(hostnames),
                )
                .group_by(TrafficHourRow.hour),
                TrafficHourRow.hour,
            )
            for hour, requests, sent, body in session.execute(totals):
                hours[hour] = {
                    "hour": hour,
                    "requests": requests,
                    "bytes_sent": sent,
                    "body_bytes_sent": body,
                    "cache": {},
                    "status": {},
                }
            cache = self._retained(
                session,
                select(
                    TrafficCacheHourRow.hour,
                    TrafficCacheHourRow.outcome,
                    func.sum(TrafficCacheHourRow.requests),
                    func.sum(TrafficCacheHourRow.bytes_sent),
                )
                .where(
                    TrafficCacheHourRow.hour >= query.start,
                    TrafficCacheHourRow.hour < query.end,
                    TrafficCacheHourRow.hostname.in_(hostnames),
                )
                .group_by(TrafficCacheHourRow.hour, TrafficCacheHourRow.outcome),
                TrafficCacheHourRow.hour,
            )
            for hour, outcome, requests, sent in session.execute(cache):
                hours[hour]["cache"][CacheOutcome(outcome)] = CacheCounters(
                    requests=requests, bytes_sent=sent
                )
            status = self._retained(
                session,
                select(
                    TrafficStatusHourRow.hour,
                    TrafficStatusHourRow.status_class,
                    func.sum(TrafficStatusHourRow.requests),
                )
                .where(
                    TrafficStatusHourRow.hour >= query.start,
                    TrafficStatusHourRow.hour < query.end,
                    TrafficStatusHourRow.hostname.in_(hostnames),
                )
                .group_by(TrafficStatusHourRow.hour, TrafficStatusHourRow.status_class),
                TrafficStatusHourRow.hour,
            )
            for hour, status_class, requests in session.execute(status):
                hours[hour]["status"][StatusClass(status_class)] = requests
            return tuple(SiteHour.model_validate(hours[hour]) for hour in sorted(hours))

    def top_paths(
        self, query: RangeQuery, *, limit: int, offset: int
    ) -> PageResult[PathCounters]:
        """Stored top paths summed over the range, most requested first."""
        if not query.hostnames:
            return PageResult(items=[], total=0)
        requests = func.sum(TrafficPathHourRow.requests)
        with self._db.session() as session:
            statement = self._retained(
                session,
                select(
                    TrafficPathHourRow.path,
                    requests,
                    func.sum(TrafficPathHourRow.bytes_sent),
                )
                .where(
                    TrafficPathHourRow.hour >= query.start,
                    TrafficPathHourRow.hour < query.end,
                    TrafficPathHourRow.hostname.in_(sorted(query.hostnames)),
                )
                .group_by(TrafficPathHourRow.path),
                TrafficPathHourRow.hour,
            )
            total = session.scalar(
                select(func.count()).select_from(statement.subquery())
            )
            rows = session.execute(
                statement.order_by(requests.desc(), TrafficPathHourRow.path)
                .limit(limit)
                .offset(offset)
            )
            return PageResult(
                items=[
                    PathCounters(path=path, requests=count, bytes_sent=sent)
                    for path, count, sent in rows
                ],
                total=total or 0,
            )

    @staticmethod
    def _retained[S: Select[Any]](session: Session, statement: S, hour: Any) -> S:
        """Hide hours behind the retention cutoff even before pruning deletes them."""
        state = session.get(TrafficRetentionRow, 1)
        return statement if state is None else statement.where(hour >= state.cutoff)

    @staticmethod
    def _cursor(row: TrafficCursorRow) -> LogCursor:
        return LogCursor(stream_id=row.stream_id, offset=row.offset, anchor=row.anchor)


def _add(
    row: object,
    delta: object,
    names: tuple[str, ...],
) -> None:
    for name in names:
        change = delta[name] if isinstance(delta, dict) else getattr(delta, name)
        total = int(getattr(row, name)) + int(change)
        if total > 2**63 - 1:
            raise ConflictError(
                "hourly traffic counter exceeds the database integer range"
            )
        setattr(row, name, total)
