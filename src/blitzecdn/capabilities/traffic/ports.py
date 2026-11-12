"""Traffic collection and storage boundaries."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from blitzecdn.capabilities.traffic.domain import (
    CollectedUsage,
    CollectionRecord,
    EdgeCollection,
    LogCursor,
    RetentionResult,
    TrafficUsage,
    UsageQuery,
)
from blitzecdn.capabilities.traffic.domain.analytics import (
    PathTotals,
    SiteBucket,
    SiteQuery,
)
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.domain.runs import AnsibleRun


class TrafficStore(Protocol):
    def collection_status(self, edge: str) -> CollectionRecord | None:
        """``edge``'s last collection attempt, or ``None`` if it was never collected."""
        ...

    def collection_statuses(self) -> Sequence[CollectionRecord]:
        """Every edge's last collection attempt, ordered by edge name."""
        ...

    def start_collection(
        self, edges: Sequence[str], attempt_id: str, started_at: datetime
    ) -> None:
        """Mark collection attempt ``attempt_id`` started on ``edges``.

        An edge whose recorded attempt started later keeps it, so an older attempt
        never overwrites a newer one.
        """
        ...

    def finish_collection(self, result: EdgeCollection, attempt_id: str) -> None:
        """Record how ``result``'s edge ended attempt ``attempt_id``.

        Ignored if a newer attempt has started on that edge since.
        """
        ...

    def prune(self, retention_days: int) -> RetentionResult:
        """Delete traffic older than ``retention_days``, a bounded amount per call.

        The result's ``more`` says expired rows remain for the next pass. Raises
        ``ValueError`` outside 1 to 3650 days.
        """
        ...

    def usage(self, query: UsageQuery) -> PageResult[TrafficUsage]:
        """One page of stored usage, cache outcomes included, matching ``query``."""
        ...

    def site_buckets(self, query: SiteQuery) -> PageResult[SiteBucket]:
        """One page of a site's totals per UTC hour or day, across edges."""
        ...

    def site_paths(self, query: SiteQuery) -> PageResult[PathTotals]:
        """A site's busiest stored paths over the range, most requested first."""
        ...

    def checkpoints(self) -> dict[str, tuple[LogCursor, ...]]:
        """Every edge's committed log cursors, where its next collection resumes."""
        ...

    def apply(
        self, edge: str, collection: CollectedUsage, *, path_limit: int = 0
    ) -> EdgeCollection:
        """Add one edge's collected batches and advance its cursors, atomically.

        A batch at or behind its committed cursor is skipped, so a repeated report
        counts once; one that contradicts a committed offset raises
        ``ConflictError``. ``path_limit`` caps the busiest paths kept per hour.
        """
        ...


class TrafficRunner(Protocol):
    def collect(
        self,
        checkpoints: dict[str, tuple[LogCursor, ...]],
        *,
        max_bytes: int,
        top_paths: int = 0,
        host_limit: str | None = None,
    ) -> AnsibleRun:
        """Ask the edges in scope for usage since each one's ``checkpoints``.

        At most ``max_bytes`` of log per edge per run; an edge with more reports
        that it has more. Takes no deployment lock.
        """
        ...


class EdgeNames(Protocol):
    """The registered edges, by name: whom a collection pass asks."""

    def __call__(self) -> Sequence[str]: ...
