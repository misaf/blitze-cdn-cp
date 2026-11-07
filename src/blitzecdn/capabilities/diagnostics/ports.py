"""Read-only inputs to operational reporting."""

from datetime import datetime
from typing import Protocol

from blitzecdn.capabilities.backup.domain import BackupRecord
from blitzecdn.capabilities.deployments.domain import ConvergenceHealth
from blitzecdn.capabilities.jobs.domain.observability import QueueStatistics


class QueueHistory(Protocol):
    def statistics(self, *, now: datetime) -> QueueStatistics: ...


class BackupHistory(Protocol):
    def latest(self) -> BackupRecord | None: ...


class ConvergenceHistory(Protocol):
    def health(self) -> tuple[ConvergenceHealth, ...]: ...
