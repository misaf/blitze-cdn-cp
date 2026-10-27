"""Commit increments and compare-and-swap log progress in one transaction."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, tuple_
from sqlalchemy.orm import Session

from blitzecdn.capabilities.traffic.adapters.tables import (
    TrafficCursorRow,
    TrafficHourRow,
    TrafficRetentionRow,
)
from blitzecdn.capabilities.traffic.domain import (
    CollectedUsage,
    EdgeCollection,
    HourlyUsage,
    LogCursor,
    RetentionResult,
    TrafficUsage,
    UsageQuery,
)
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.exceptions import ConflictError
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.persistence.pagination import read_page


class TrafficStore:
    def __init__(self, database: Database) -> None:
        self._db = database

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
            return read_page(
                session,
                statement,
                lambda row: TrafficUsage(
                    edge=row.edge,
                    hostname=row.hostname,
                    hour=row.hour,
                    requests=row.requests,
                    bytes_sent=row.bytes_sent,
                    body_bytes_sent=row.body_bytes_sent,
                ),
                limit=query.limit,
                offset=query.offset,
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
        row = session.get(TrafficHourRow, (edge, bucket.hostname, bucket.hour))
        if row is None:
            session.add(TrafficHourRow(edge=edge, **bucket.model_dump()))
        else:
            for name in ("requests", "bytes_sent", "body_bytes_sent"):
                total = int(getattr(row, name)) + int(getattr(bucket, name))
                if total > 2**63 - 1:
                    raise ConflictError(
                        "hourly traffic counter exceeds the database integer range"
                    )
                setattr(row, name, total)
        session.flush()

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
            )
            return tuple(
                HourlyUsage(
                    hostname=row.hostname,
                    hour=row.hour,
                    requests=row.requests,
                    bytes_sent=row.bytes_sent,
                    body_bytes_sent=row.body_bytes_sent,
                )
                for row in rows
            )

    @staticmethod
    def _cursor(row: TrafficCursorRow) -> LogCursor:
        return LogCursor(stream_id=row.stream_id, offset=row.offset, anchor=row.anchor)
