"""Read-only inputs to operational reporting."""

from datetime import datetime
from typing import Protocol

from blitzecdn.capabilities.jobs.domain.observability import QueueStatistics


class QueueHistory(Protocol):
    def statistics(self, *, now: datetime) -> QueueStatistics: ...
