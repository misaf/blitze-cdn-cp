"""Published hourly usage counters."""

from datetime import datetime

from blitzecdn.entrypoints.api.models import Model


class TrafficUsage(Model):
    edge: str
    hostname: str
    hour: datetime
    requests: int
    bytes_sent: int
    body_bytes_sent: int
