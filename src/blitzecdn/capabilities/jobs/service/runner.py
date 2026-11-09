"""The loop a worker process runs: sleep when idle, stop when asked.

Polling, not push: an idle poll is one indexed SELECT, cheaper than a broker.
While a job runs, a heartbeat renews its lease, so a run that outlasts one lease
is not taken over by another worker; a dead worker stops renewing and its job
becomes claimable when the last renewal lapses.
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
        heartbeat_seconds: float | None = None,
    ) -> None:
        self.queue = queue
        #: How often a running job's lease is renewed; a third of the lease held
        #: when ``None``, so two renewals can be missed before it lapses.
        self.heartbeat_seconds = heartbeat_seconds
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
        held = self._hold_for_declared_lease(claim)
        if self.progress is not None:
            self.progress.busy(claim)
        try:
            with _LeaseHeartbeat(
                self.queue,
                claim,
                lease_seconds=held,
                interval=self.heartbeat_seconds or held / 3,
            ):
                self.queue.run(claim)
        finally:
            if self.progress is not None:
                self.progress.idle()
        # Pruned while processing work; pending and running jobs are never pruned.
        self.queue.prune()
        return True

    def _hold_for_declared_lease(self, claim: JobClaim) -> int:
        """Extend a claim to the lease its schedule or operation kind declares.

        Claims start with the default lease; renewed under the fence right after.
        Returns the lease now held, which the heartbeat keeps renewing.
        """
        job = claim.job
        if job.kind == ScheduledFiring.kind:
            try:
                firing = ScheduledFiring.model_validate(job.payload)
            except ValidationError:
                # Running it fails the job and says why; no lease to declare.
                return self.queue.lease_seconds
            declared = self.scheduler.leases().get(firing.job)
        else:
            # An operation's lease only ever lengthens the default one.
            declared = max(
                self.operation_leases.get(job.kind, 0), self.queue.lease_seconds
            )
        if declared:
            self.queue.renew(claim, seconds=declared)
            return declared
        return self.queue.lease_seconds

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


class _LeaseHeartbeat:
    """Renew a claim's lease on a timer for as long as its job runs.

    Stops for good once a renewal is refused: the fence moved, so another worker
    owns the job and this one's outcome will be discarded anyway. A renewal that
    raises is logged and retried on the next beat; the lease covers the gap.
    """

    def __init__(
        self, queue: JobQueue, claim: JobClaim, *, lease_seconds: int, interval: float
    ) -> None:
        self._queue = queue
        self._claim = claim
        self._lease_seconds = lease_seconds
        self._interval = max(interval, 0.001)
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._beat,
            name=f"lease-heartbeat-{claim.job.id}",
            daemon=True,
        )

    def __enter__(self) -> _LeaseHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._done.set()
        self._thread.join()

    def _beat(self) -> None:
        job = self._claim.job
        while not self._done.wait(self._interval):
            try:
                renewed = self._queue.renew(self._claim, seconds=self._lease_seconds)
            except Exception:
                _LOGGER.exception("renewing the lease on job %s failed", job.id)
                continue
            if not renewed:
                _LOGGER.warning(
                    "job %s lost its lease while running; another worker may "
                    "own it now, so its lease is no longer renewed",
                    job.id,
                )
                return
