"""Published hourly usage counters."""

from datetime import datetime

from blitzecdn.capabilities.traffic.domain import FreshnessState
from blitzecdn.entrypoints.api.models import Model


class TrafficUsage(Model):
    edge: str
    hostname: str
    hour: datetime
    requests: int
    bytes_sent: int
    body_bytes_sent: int


class CollectionStatus(Model):
    edge: str
    registered: bool
    collection_enabled: bool
    interval_seconds: int
    stale_after_seconds: int
    checked_at: datetime
    state: FreshnessState
    stale: bool
    last_attempt_started_at: datetime | None = None
    last_attempt_finished_at: datetime | None = None
    last_success_at: datetime | None = None
    last_caught_up_at: datetime | None = None
    pending: bool | None = None
    last_error: str | None = None
