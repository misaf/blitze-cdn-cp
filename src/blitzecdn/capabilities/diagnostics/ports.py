"""Read-only inputs to operational reporting."""

from datetime import datetime
from typing import Protocol

from blitzecdn.capabilities.backup import BackupRecord
from blitzecdn.capabilities.deployments import ConvergenceHealth
from blitzecdn.capabilities.jobs import QueueStatistics


class QueueHistory(Protocol):
    def statistics(self, *, now: datetime) -> QueueStatistics:
        """Job counts by status, and how far the ready backlog lags ``now``."""
        ...


class BackupHistory(Protocol):
    def latest(self) -> BackupRecord | None:
        """The last completed backup, or ``None`` when there is none to trust."""
        ...


class ConvergenceHistory(Protocol):
    def health(self) -> tuple[ConvergenceHealth, ...]:
        """Every convergence kind's record, in declaration order."""
        ...
