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
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.domain.runs import AnsibleRun


class TrafficStore(Protocol):
    def collection_status(self, edge: str) -> CollectionRecord | None: ...

    def start_collection(
        self, edges: Sequence[str], attempt_id: str, started_at: datetime
    ) -> None: ...

    def finish_collection(self, result: EdgeCollection, attempt_id: str) -> None: ...

    def prune(self, retention_days: int) -> RetentionResult: ...

    def usage(self, query: UsageQuery) -> PageResult[TrafficUsage]: ...

    def checkpoints(self) -> dict[str, tuple[LogCursor, ...]]: ...

    def apply(self, edge: str, collection: CollectedUsage) -> EdgeCollection: ...


class TrafficRunner(Protocol):
    def collect(
        self,
        checkpoints: dict[str, tuple[LogCursor, ...]],
        *,
        host_limit: str | None = None,
    ) -> AnsibleRun: ...
