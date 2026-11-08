"""How well the cache is working: stored hourly outcomes and live connections.

Cache outcomes are counted by traffic accounting, which reads every log record
exactly once and stores it per edge, hostname and UTC hour. This module only
judges them. Connection counters come live from nginx's ``stub_status``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from blitzecdn.capabilities.traffic.domain import (
    CACHE_CONSULTED_OUTCOMES,
    CACHE_HIT_OUTCOMES,
)

__all__ = [
    "CACHE_CONSULTED_OUTCOMES",
    "CACHE_HIT_OUTCOMES",
    "CacheHour",
    "ConnectionsReport",
    "EdgeConnections",
    "OutcomeCounters",
]


def _ratio(part: int, whole: int) -> float | None:
    """None for nothing consulted: idle is not broken."""
    return round(part / whole, 4) if whole else None


class OutcomeCounters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requests: int
    bytes_sent: int


class CacheHour(BaseModel):
    """One hostname's cache outcomes on one edge in one UTC hour."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    edge: str
    hostname: str
    hour: datetime
    outcomes: dict[str, OutcomeCounters] = Field(default_factory=dict)

    def _sum(self, field: str, among: frozenset[str] | None = None) -> int:
        return sum(
            getattr(counters, field)
            for outcome, counters in self.outcomes.items()
            if among is None or outcome in among
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def requests(self) -> int:
        """Every logged request, including those that never used the cache."""
        return self._sum("requests")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cacheable_requests(self) -> int:
        return self._sum("requests", CACHE_CONSULTED_OUTCOMES)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hits(self) -> int:
        return self._sum("requests", CACHE_HIT_OUTCOMES)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hit_ratio(self) -> float | None:
        """Hits over cache-consulting requests."""
        return _ratio(self.hits, self.cacheable_requests)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def bytes_sent(self) -> int:
        return self._sum("bytes_sent")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cached_bytes_sent(self) -> int:
        """Bytes served from cache: what the origin did not have to send."""
        return self._sum("bytes_sent", CACHE_HIT_OUTCOMES)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def byte_hit_ratio(self) -> float | None:
        """Origin offload: cached bytes over bytes of cache-consulting requests."""
        return _ratio(
            self.cached_bytes_sent, self._sum("bytes_sent", CACHE_CONSULTED_OUTCOMES)
        )


class EdgeConnections(BaseModel):
    """One edge's live nginx counters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    collected_at: datetime | None = None
    nginx_reachable: bool = False
    #: stub_status counters, cumulative since nginx started. Deltas need two
    #: readings, which this does not attempt to store.
    connections: dict[str, int] = Field(default_factory=dict)
    #: Set when the edge produced no usable report at all.
    error: str | None = None


class ConnectionsReport(BaseModel):
    """Live connection counters across the fleet, as of one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    collected_at: datetime
    host_limit: str | None = None
    edges: tuple[EdgeConnections, ...] = ()

    @property
    def reporting(self) -> tuple[EdgeConnections, ...]:
        return tuple(edge for edge in self.edges if edge.error is None)

    @property
    def silent(self) -> tuple[EdgeConnections, ...]:
        return tuple(edge for edge in self.edges if edge.error is not None)
