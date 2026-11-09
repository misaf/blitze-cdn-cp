"""Commit increments and compare-and-swap log progress in one transaction."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, String, delete, func, select, tuple_, type_coerce
from sqlalchemy.orm import Session

from blitzecdn.capabilities.traffic.adapters.tables import (
    TrafficCacheHourRow,
    TrafficCollectionRow,
    TrafficCursorRow,
    TrafficHourRow,
    TrafficPathHourRow,
    TrafficRetentionRow,
)
from blitzecdn.capabilities.traffic.domain import (
    CacheCounters,
    CacheOutcome,
    CollectedUsage,
    CollectionRecord,
    EdgeCollection,
    HourlyUsage,
    LogCursor,
    PathCounters,
    RetentionResult,
    StatusClass,
    TrafficUsage,
    UsageQuery,
)
from blitzecdn.capabilities.traffic.domain.analytics import (
    Granularity,
    OutcomeTotals,
    PathTotals,
    SiteBucket,
    SiteQuery,
)
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.exceptions import ConflictError
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.persistence.pagination import read_page

type _HourKey = tuple[str, str, datetime]
type _Shares = dict[CacheOutcome, CacheCounters]

#: The ``traffic_hours`` column holding each status class's requests.
_STATUS_COLUMNS = {
    StatusClass.INFORMATIONAL: "status_1xx",
    StatusClass.SUCCESS: "status_2xx",
    StatusClass.REDIRECTION: "status_3xx",
    StatusClass.CLIENT_ERROR: "status_4xx",
    StatusClass.SERVER_ERROR: "status_5xx",
}


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

    def apply(
        self, edge: str, collection: CollectedUsage, *, path_limit: int = 0
    ) -> EdgeCollection:
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
                    self._increment(session, edge, bucket, path_limit)
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
    def _increment(
        session: Session, edge: str, bucket: HourlyUsage, path_limit: int
    ) -> None:
        key = (edge, bucket.hostname, bucket.hour)
        row = session.get(TrafficHourRow, key)
        statuses = {
            column: bucket.status.get(status, 0)
            for status, column in _STATUS_COLUMNS.items()
        }
        if row is None:
            session.add(
                TrafficHourRow(
                    edge=edge,
                    **bucket.model_dump(exclude={"cache", "status", "paths"}),
                    **statuses,
                )
            )
        else:
            _add(row, bucket, ("requests", "bytes_sent", "body_bytes_sent"))
            for column, count in statuses.items():
                _increase(row, column, count)
        session.flush()
        if path_limit and bucket.paths:
            TrafficStore._increment_paths(session, key, bucket, path_limit)
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
        session.flush()

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
    def _usage(row: TrafficHourRow, outcomes: dict[_HourKey, _Shares]) -> HourlyUsage:
        return HourlyUsage(
            hostname=row.hostname,
            hour=row.hour,
            requests=row.requests,
            bytes_sent=row.bytes_sent,
            body_bytes_sent=row.body_bytes_sent,
            cache=outcomes[(row.edge, row.hostname, row.hour)],
            status={
                status: getattr(row, column)
                for status, column in _STATUS_COLUMNS.items()
                if getattr(row, column)
            },
        )

    @staticmethod
    def _increment_paths(
        session: Session, key: _HourKey, bucket: HourlyUsage, path_limit: int
    ) -> None:
        """Add the batch's busiest paths, then keep only the hour's busiest."""
        for item in bucket.paths:
            share = session.get(TrafficPathHourRow, (*key, item.path))
            if share is None:
                session.add(
                    TrafficPathHourRow(
                        edge=key[0], hostname=key[1], hour=key[2], **item.model_dump()
                    )
                )
            else:
                _add(share, item, ("requests", "bytes_sent"))
        session.flush()
        in_hour = (
            TrafficPathHourRow.edge == key[0],
            TrafficPathHourRow.hostname == key[1],
            TrafficPathHourRow.hour == key[2],
        )
        kept = (
            select(TrafficPathHourRow.path)
            .where(*in_hour)
            .order_by(TrafficPathHourRow.requests.desc(), TrafficPathHourRow.path)
            .limit(path_limit)
        )
        session.execute(
            delete(TrafficPathHourRow).where(
                *in_hour, TrafficPathHourRow.path.not_in(kept)
            ),
            # Another batch of this hour in the same transaction must not find
            # a trimmed row in the session and add to it.
            execution_options={"synchronize_session": "fetch"},
        )

    def site_buckets(self, query: SiteQuery) -> PageResult[SiteBucket]:
        """One page of a site's hourly or daily totals, summed in SQL."""
        bucket = self._bucket(TrafficHourRow.hour, query.granularity)
        statement = self._in_range(
            select(
                bucket.label("bucket"),
                func.sum(TrafficHourRow.requests),
                func.sum(TrafficHourRow.bytes_sent),
                func.sum(TrafficHourRow.body_bytes_sent),
                *(
                    func.sum(getattr(TrafficHourRow, column))
                    for column in _STATUS_COLUMNS.values()
                ),
            ),
            TrafficHourRow,
            query,
        )
        statement = statement.group_by(bucket).order_by(bucket)
        with self._db.session() as session:
            statement = self._retained(session, statement, TrafficHourRow)
            page = read_page(
                session,
                statement,
                lambda row: row,
                limit=query.limit,
                offset=query.offset,
            )
            keys = [row[0] for row in page.items]
            cache = self._bucket_outcomes(session, query, keys)
            return PageResult(
                items=[
                    SiteBucket(
                        start=self._bucket_start(row[0], query.granularity),
                        requests=row[1],
                        bytes_sent=row[2],
                        body_bytes_sent=row[3],
                        cache=cache.get(row[0], {}),
                        status={
                            status: count
                            for status, count in zip(
                                _STATUS_COLUMNS, row[4:], strict=True
                            )
                            if count
                        },
                    )
                    for row in page.items
                ],
                total=page.total,
            )

    def site_paths(self, query: SiteQuery) -> PageResult[PathTotals]:
        """A site's busiest paths over the range, most requested first."""
        requests = func.sum(TrafficPathHourRow.requests)
        statement: Select[Any] = self._in_range(
            select(
                TrafficPathHourRow.path,
                requests,
                func.sum(TrafficPathHourRow.bytes_sent),
            ),
            TrafficPathHourRow,
            query,
        )
        statement = statement.group_by(TrafficPathHourRow.path).order_by(
            requests.desc(), TrafficPathHourRow.path
        )
        with self._db.session() as session:
            statement = self._retained(session, statement, TrafficPathHourRow)
            page = read_page(
                session,
                statement,
                lambda row: PathTotals(path=row[0], requests=row[1], bytes_sent=row[2]),
                limit=query.limit,
                offset=query.offset,
            )
            return PageResult(items=page.items, total=page.total)

    def _bucket_outcomes(
        self, session: Session, query: SiteQuery, keys: Sequence[str]
    ) -> dict[str, dict[CacheOutcome, OutcomeTotals]]:
        grouped: dict[str, dict[CacheOutcome, OutcomeTotals]] = {}
        if not keys:
            return grouped
        bucket = self._bucket(TrafficCacheHourRow.hour, query.granularity)
        statement = self._in_range(
            select(
                bucket,
                TrafficCacheHourRow.outcome,
                func.sum(TrafficCacheHourRow.requests),
                func.sum(TrafficCacheHourRow.bytes_sent),
            ),
            TrafficCacheHourRow,
            query,
        ).group_by(bucket, TrafficCacheHourRow.outcome)
        statement = self._retained(session, statement, TrafficCacheHourRow)
        wanted = set(keys)
        for key, outcome, requests, bytes_sent in session.execute(statement):
            if key in wanted:
                grouped.setdefault(key, {})[CacheOutcome(outcome)] = OutcomeTotals(
                    requests=requests, bytes_sent=bytes_sent
                )
        return grouped

    @staticmethod
    def _bucket(column: object, granularity: Granularity) -> Any:
        """Hours are ISO-8601 UTC text, so a day is its first ten characters."""
        if granularity is Granularity.DAY:
            return func.substr(type_coerce(column, String), 1, 10)
        return type_coerce(column, String)

    @staticmethod
    def _bucket_start(key: str, granularity: Granularity) -> datetime:
        if granularity is Granularity.DAY:
            return datetime.fromisoformat(key).replace(tzinfo=UTC)
        return datetime.fromisoformat(key)

    @staticmethod
    def _in_range[S: Select[Any]](
        statement: S,
        table: type[TrafficHourRow | TrafficCacheHourRow | TrafficPathHourRow],
        query: SiteQuery,
    ) -> S:
        statement = statement.where(
            table.hostname == query.site,
            table.hour >= query.start,
            table.hour < query.end,
        )
        if query.edge is not None:
            statement = statement.where(table.edge == query.edge)
        return statement

    @staticmethod
    def _retained[S: Select[Any]](
        session: Session,
        statement: S,
        table: type[TrafficHourRow | TrafficCacheHourRow | TrafficPathHourRow],
    ) -> S:
        """Expired hours are hidden at once, before pruning deletes them."""
        state = session.get(TrafficRetentionRow, 1)
        if state is None:
            return statement
        return statement.where(table.hour >= state.cutoff)

    def hourly(
        self, *, edge: str, hostname: str, start: datetime, end: datetime
    ) -> tuple[HourlyUsage, ...]:
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

    @staticmethod
    def _cursor(row: TrafficCursorRow) -> LogCursor:
        return LogCursor(stream_id=row.stream_id, offset=row.offset, anchor=row.anchor)


def _add(
    row: TrafficHourRow | TrafficCacheHourRow | TrafficPathHourRow,
    delta: HourlyUsage | CacheCounters | PathCounters,
    names: tuple[str, ...],
) -> None:
    for name in names:
        _increase(row, name, int(getattr(delta, name)))


def _increase(
    row: TrafficHourRow | TrafficCacheHourRow | TrafficPathHourRow,
    name: str,
    amount: int,
) -> None:
    total = int(getattr(row, name)) + amount
    if total > 2**63 - 1:
        raise ConflictError("hourly traffic counter exceeds the database integer range")
    setattr(row, name, total)
