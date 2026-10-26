"""Worker heartbeats on the controller's shared state volume.

The pulse reports process liveness independently of a blocking job. It never
renews job leases and cannot establish that a remote operation is progressing.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import timedelta
from pathlib import Path
from time import monotonic

from blitzecdn.capabilities.jobs.domain import JobClaim
from blitzecdn.capabilities.jobs.domain.observability import (
    WorkerHeartbeat,
    WorkerObservations,
)
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.ports.clock import Clock, system_clock
from blitzecdn.core.runtime.filesystem import atomic_write_bytes

_LOGGER = logging.getLogger(__name__)


class WorkerMonitor:
    def __init__(
        self,
        directory: Path,
        *,
        clock: Clock = system_clock,
        interval: float = 5,
    ) -> None:
        self.directory = directory
        self.clock = clock
        self.interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: WorkerHeartbeat | None = None
        self._path: Path | None = None
        self._job_started = 0.0

    def read(self) -> WorkerObservations:
        workers = []
        unreadable = 0
        try:
            paths = sorted(self.directory.iterdir())
        except FileNotFoundError:
            return WorkerObservations()
        except OSError:
            return WorkerObservations(unreadable=1)
        for path in paths:
            if path.suffix != ".json":
                continue
            try:
                if path.is_symlink():
                    raise ValueError("worker record is a symlink")
                workers.append(WorkerHeartbeat.model_validate_json(path.read_bytes()))
            except (OSError, ValueError):
                unreadable += 1
        return WorkerObservations(workers=tuple(workers), unreadable=unreadable)

    def start(self, worker_id: str) -> None:
        if self._thread is not None:
            return
        self._prune()
        identity = hashlib.sha256(worker_id.encode()).hexdigest()
        self._path = self.directory / f"{identity}.json"
        self._state = WorkerHeartbeat(worker_id=worker_id, heartbeat_at=self.clock())
        self._stop.clear()
        self._pulse()
        self._thread = threading.Thread(
            target=self._watch,
            name="blitzecdn-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def busy(self, claim: JobClaim) -> None:
        with self._lock:
            if self._state is None:
                return
            self._job_started = monotonic()
            self._state = self._state.model_copy(
                update={
                    "current_job_id": claim.job.id,
                    "current_kind": claim.job.kind,
                    "job_started_at": self.clock(),
                }
            )
        self._pulse()

    def idle(self) -> None:
        with self._lock:
            if self._state is None or self._state.current_job_id is None:
                return
            self._state = self._state.model_copy(
                update={
                    "current_job_id": None,
                    "current_kind": None,
                    "job_started_at": None,
                    "last_finished_at": self.clock(),
                    "last_duration_seconds": max(0, monotonic() - self._job_started),
                }
            )
        self._pulse()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        with self._lock:
            self._state = None
            if self._path is not None:
                try:
                    self._path.unlink(missing_ok=True)
                except OSError:
                    _LOGGER.exception("could not remove worker heartbeat")
                self._path = None

    def _watch(self) -> None:
        while not self._stop.wait(self.interval):
            self._pulse()

    def _pulse(self) -> None:
        with self._lock:
            if self._state is None or self._path is None:
                return
            self._state = self._state.model_copy(update={"heartbeat_at": self.clock()})
            try:
                atomic_write_bytes(self._path, self._state.model_dump_json().encode())
            except (OSError, ConfigurationError):
                _LOGGER.exception("could not publish worker heartbeat")

    def _prune(self) -> None:
        """Keep crash evidence for a day; graceful stops remove their own record."""
        cutoff = self.clock() - timedelta(days=1)
        for path in self.directory.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff.timestamp():
                    path.unlink()
            except OSError:
                _LOGGER.exception("could not prune old worker heartbeat")
