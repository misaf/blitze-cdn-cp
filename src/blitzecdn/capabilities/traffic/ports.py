"""Traffic collection and storage boundaries."""

from typing import Protocol

from blitzecdn.capabilities.traffic.domain import (
    CollectedUsage,
    EdgeCollection,
    LogCursor,
    RetentionResult,
    TrafficUsage,
    UsageQuery,
)
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.domain.runs import AnsibleRun


class TrafficStore(Protocol):
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
