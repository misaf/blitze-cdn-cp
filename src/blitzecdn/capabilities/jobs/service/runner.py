"""The loop a worker process runs: sleep when idle, stop when asked.

Polling, not push: an idle poll is one indexed SELECT, cheaper than a broker.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable, Mapping
from types import FrameType

from pydantic import ValidationError

from blitzecdn.capabilities.jobs.domain import JobClaim, ScheduledFiring
from blitzecdn.capabilities.jobs.ports import WorkerProgress
from blitzecdn.capabilities.jobs.service.queue import JobQueue
from blitzecdn.capabilities.jobs.service.scheduling import JobScheduler
from blitzecdn.core.ports.clock import Clock

__all__ = ["JobRunner"]

_LOGGER = logging.getLogger(__name__)


class JobRunner:
    """Claim, run, repeat — with the schedule ticked on the way round."""

    def __init__(
        self,
        *,
        queue: JobQueue,
        scheduler: JobScheduler,
        poll_seconds: float = 1.0,
        clock: Clock,
        progress: WorkerProgress | None = None,
        operation_leases: Mapping[str, int] | None = None,
    ) -> None:
        self.queue = queue
        self.operation_leases = dict(operation_leases or {})
        self.scheduler = scheduler
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.progress = progress
        self._stop = threading.Event()

    def start_reporting(self) -> None:
        if self.progress is not None:
            self.progress.start(self.queue.worker_id)

    def stop_reporting(self) -> None:
        if self.progress is not None:
            self.progress.stop()

    def stop(self) -> None:
        """Ask the loop to finish the job in hand and then return."""
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """Turn SIGTERM and SIGINT into a request to stop after the current job."""

        def handle(signum: int, _frame: FrameType | None) -> None:
            _LOGGER.info("signal %s received; stopping after the current job", signum)
            self.stop()

        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, handle)

    def tick(self) -> bool:
        """One turn of the loop, scheduling first; ``True`` when it did something."""
        fired = self.scheduler.tick(now=self.clock())
        claim = self.queue.claim()
        if claim is None:
            return bool(fired)
        self._hold_for_declared_lease(claim)
        if self.progress is not None:
            self.progress.busy(claim)
        try:
            self.queue.run(claim)
        finally:
            if self.progress is not None:
                self.progress.idle()
        # Pruned while processing work; pending and running jobs are never pruned.
        self.queue.prune()
        return True

    def _hold_for_declared_lease(self, claim: JobClaim) -> None:
        """Extend a claim to the lease its schedule or operation kind declares.

        Claims start with the default lease; renewed under the fence right after.
        """
        job = claim.job
        if job.kind == ScheduledFiring.kind:
            try:
                firing = ScheduledFiring.model_validate(job.payload)
            except ValidationError:
                # Running it fails the job and says why; no lease to declare.
                return
            declared = self.scheduler.leases().get(firing.job)
        else:
            # An operation's lease only ever lengthens the default one.
            declared = max(
                self.operation_leases.get(job.kind, 0), self.queue.lease_seconds
            )
        if declared:
            self.queue.renew(claim, seconds=declared)

    def run_forever(self) -> None:
        """Tick until asked to stop, sleeping on the stop event when idle."""
        while not self._stop.is_set():
            try:
                busy = self.tick()
            except Exception:
                # Keep polling through errors around jobs; job failures are on their
                # rows.
                _LOGGER.exception("worker loop iteration failed; continuing")
                busy = False
            if not busy:
                self._stop.wait(self.poll_seconds)

    def run_until(self, predicate: Callable[[], bool]) -> None:
        """Tick until ``predicate`` says to stop. The bounded form, for tests."""
        while not predicate() and not self._stop.is_set():
            if not self.tick():
                self._stop.wait(self.poll_seconds)
