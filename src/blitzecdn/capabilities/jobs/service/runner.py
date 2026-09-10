"""The loop a worker process runs, and the only thing that makes it a process.

Everything above this is a function of a clock and a table, so all of it can be
tested by calling it. What is left here is the part that genuinely needs a
process: sleeping when there is nothing to do, and stopping when asked.

Polling rather than a notification. A queue that pushes needs a broker to push
through, and the workload this serves — one convergence at a time, and a
handful of scheduled jobs on hourly intervals — is one where an idle poll every
second costs a single indexed SELECT against a local file. The push machinery
would cost a network service, its supervision, and a second place for state to
be.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from types import FrameType
from typing import Any

from blitzecdn.capabilities.jobs.domain import utcnow
from blitzecdn.capabilities.jobs.service.queue import JobQueue
from blitzecdn.capabilities.jobs.service.scheduling import JobScheduler

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
        clock: Any = utcnow,
    ) -> None:
        self.queue = queue
        self.scheduler = scheduler
        self.poll_seconds = poll_seconds
        self.clock = clock
        self._stop = threading.Event()

    def stop(self) -> None:
        """Ask the loop to finish the job in hand and then return."""
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """Turn SIGTERM and SIGINT into a request to stop after this job.

        A container stop sends SIGTERM, and the default action would kill the
        process mid-convergence. The job would survive — its lease expires and
        another worker takes it over — but finishing the one in hand is both
        faster and easier to read in the log than a lease timing out.
        """

        def handle(signum: int, _frame: FrameType | None) -> None:
            _LOGGER.info("signal %s received; stopping after the current job", signum)
            self.stop()

        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, handle)

    def tick(self) -> bool:
        """One turn of the loop. ``True`` when it did something.

        Scheduling first, so a schedule that came due while the last job was
        running is queued before this turn looks for work — otherwise a busy
        worker defers every recurring job by the length of whatever it happens
        to be doing.
        """
        fired = self.scheduler.tick(now=self.clock())
        ran = self.queue.run_one()
        return bool(fired) or ran is not None

    def run_forever(self) -> None:
        """Tick until asked to stop, sleeping only when there was nothing to do.

        The sleep is on the stop event rather than on the clock, so a stop
        request is acted on immediately instead of after the poll interval — a
        container shutdown should not have to wait out an idle sleep.
        """
        while not self._stop.is_set():
            try:
                busy = self.tick()
            except Exception:
                # A loop that dies on an unexpected error is a worker that
                # silently stops doing every other job too. Individual job
                # failures are already recorded on their rows; this catches
                # what happens *around* them — a database that went away, a
                # schedule that could not be read — and keeps polling.
                _LOGGER.exception("worker loop iteration failed; continuing")
                busy = False
            if not busy:
                self._stop.wait(self.poll_seconds)

    def run_until(self, predicate: Callable[[], bool]) -> None:
        """Tick until ``predicate`` says to stop. The bounded form, for tests."""
        while not predicate() and not self._stop.is_set():
            if not self.tick():
                self._stop.wait(self.poll_seconds)
