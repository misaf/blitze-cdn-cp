"""Enqueueing durable work, and running one item of it correctly.

A claim is leased and fenced; failures retry with bounded backoff, then fail
and say why; duplicate delivery is the handlers' to tolerate, so exactly-once
is not attempted.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from blitzecdn.capabilities.jobs.domain import (
    DEFAULT_LEASE_SECONDS,
    Defer,
    Done,
    Fail,
    Job,
    JobClaim,
    JobStatus,
    Retry,
    backoff_for,
)
from blitzecdn.capabilities.jobs.ports import JobHandler, JobStore
from blitzecdn.core.ports.clock import Clock, system_clock

__all__ = ["DEPLOYMENT_JOB", "SCHEDULED_JOB", "JobQueue"]

_LOGGER = logging.getLogger(__name__)
_DEFERRAL_SECONDS = 5

#: The queued work kinds and their payload keys: a durable cross-process contract.
DEPLOYMENT_JOB = "deployment"
SCHEDULED_JOB = "scheduled"


class JobQueue:
    """The durable queue, and the one place a job's outcome is recorded."""

    def __init__(
        self,
        *,
        jobs: JobStore,
        handlers: Mapping[str, JobHandler],
        worker_id: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        retention: int = 200,
        clock: Clock = system_clock,
    ) -> None:
        self.jobs = jobs
        #: Kind to handler; the worker learns nothing else about a job.
        self.handlers = dict(handlers)
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.retention = retention
        #: Injected so tests can move time.
        self.clock = clock

    def consume_with(self, handlers: Mapping[str, JobHandler]) -> None:
        """Hand the queue its handlers once, after the services they call exist."""
        if self.handlers:
            raise RuntimeError("this queue already has its handlers")
        self.handlers = dict(handlers)

    # -- Publishing ----------------------------------------------------

    def enqueue_deployment(self, deployment_id: str) -> Job | None:
        """Queue a convergence for the worker, deduplicated on the deployment id."""
        return self.jobs.enqueue(
            kind=DEPLOYMENT_JOB,
            payload={"deployment_id": deployment_id},
            available_at=self.clock(),
            dedupe_key=f"{DEPLOYMENT_JOB}:{deployment_id}",
        )

    def enqueue_scheduled(
        self, job: str, *, available_at: datetime | None = None, max_attempts: int = 1
    ) -> Job | None:
        """Queue one firing of a recurring job, single-flight and with one attempt.

        Recurring work retries by recurring, against fresh state.
        """
        return self.jobs.enqueue(
            kind=SCHEDULED_JOB,
            payload={"job": job},
            available_at=available_at or self.clock(),
            dedupe_key=f"{SCHEDULED_JOB}:{job}",
            max_attempts=max_attempts,
        )

    # -- Consuming -----------------------------------------------------

    def claim(self, *, lease_seconds: int | None = None) -> JobClaim | None:
        """Take the oldest runnable job, or ``None`` when there is none."""
        return self.jobs.claim(
            worker=self.worker_id,
            now=self.clock(),
            lease_seconds=lease_seconds or self.lease_seconds,
        )

    def run_one(self, *, lease_seconds: int | None = None) -> Job | None:
        """Claim one job, run it, and return it as claimed, or ``None`` if empty."""
        claim = self.claim(lease_seconds=lease_seconds)
        if claim is None:
            return None
        self.run(claim)
        return claim.job

    def run(self, claim: JobClaim) -> None:
        """Run a claimed job under its fence and record what its handler said.

        ``Done`` completes, ``Defer`` requeues free, ``Retry`` backs off spending an
        attempt, ``Fail`` fails now; an escaping exception counts as ``Retry``. A kind
        with no handler fails permanently, naming the kind.
        """
        job = claim.job
        handler = self.handlers.get(job.kind)
        if handler is None:
            self._fail(
                claim,
                f"no handler is registered for job kind {job.kind!r}; the "
                "feature that contributed it may have been removed",
                permanent=True,
            )
            return
        try:
            outcome = handler(job.payload)
        except Exception as exc:
            _LOGGER.exception("job %s (%s) failed", job.id, job.kind)
            self._fail(claim, f"{type(exc).__name__}: {exc}", permanent=False)
            return
        match outcome:
            case Done():
                pass
            case Defer(reason=reason):
                self._defer(claim, reason)
                return
            case Retry(reason=reason):
                _LOGGER.warning(
                    "job %s (%s) will be retried: %s", job.id, job.kind, reason
                )
                self._fail(claim, reason, permanent=False)
                return
            case Fail(reason=reason):
                _LOGGER.warning(
                    "job %s (%s) failed permanently: %s", job.id, job.kind, reason
                )
                self._fail(claim, reason, permanent=True)
                return
            case _:
                self._fail(
                    claim,
                    f"the {job.kind!r} handler returned {outcome!r}, not a job outcome",
                    permanent=True,
                )
                return
        if not self.jobs.complete(job.id, claim.fence, now=self.clock()):
            # Someone else took the job over meanwhile; record nothing for it.
            _LOGGER.warning(
                "job %s finished under an expired lease; another worker owns "
                "it now and this result was discarded",
                job.id,
            )

    def renew(self, claim: JobClaim, *, seconds: int | None = None) -> bool:
        """Extend a lease this worker still holds, for a long-running job."""
        return self.jobs.renew(
            claim.job.id,
            claim.fence,
            until=self.clock() + timedelta(seconds=seconds or self.lease_seconds),
        )

    # -- Reading -------------------------------------------------------

    def get(self, job_id: str) -> Job:
        return self.jobs.get(job_id)

    def list_jobs(
        self, *, status: JobStatus | None = None, limit: int = 50
    ) -> Sequence[Job]:
        return self.jobs.list_jobs(status=status, limit=limit)

    def prune(self) -> int:
        return self.jobs.prune(self.retention)

    # -- Internals -----------------------------------------------------

    def _defer(self, claim: JobClaim, reason: str) -> None:
        job = claim.job
        deferred = self.jobs.defer(
            job.id,
            claim.fence,
            retry_at=self.clock() + timedelta(seconds=_DEFERRAL_SECONDS),
            reason=reason,
        )
        if deferred:
            _LOGGER.info(
                "job %s deferred without an execution attempt: %s", job.id, reason
            )
        else:
            _LOGGER.warning("job %s deferral refused: its claim is stale", job.id)

    def _fail(self, claim: JobClaim, error: str, *, permanent: bool) -> None:
        now = self.clock()
        job = claim.job
        # `attempts` was already incremented by the claim, so this compares the
        # attempt that just failed rather than the one before it.
        retry_at = (
            None if permanent or job.exhausted else now + backoff_for(job.attempts)
        )
        self.jobs.fail(job.id, claim.fence, now=now, error=error, retry_at=retry_at)
