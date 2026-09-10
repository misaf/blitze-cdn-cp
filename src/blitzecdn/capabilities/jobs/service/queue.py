"""Enqueueing durable work, and running one item of it correctly.

The rules that make "at least once" safe to build on live here, and there are
only three of them.

**A claim is leased and fenced.** The worker gets a job and a fence token; every
write it makes afterwards names that token. A worker that was paused past its
lease — a long stop-the-world pause, a suspended container, a partition it never
noticed — comes back to find its token stale and is refused. It cannot mark as
succeeded a job another worker has since re-run, which is the failure a lease
alone still allows.

**A failure retries with bounded backoff until its attempts are spent.** Then it
fails permanently and says why. A job that retried forever would be a job nobody
is ever told about.

**Duplicate delivery is the handler's problem, and the handlers were built for
it.** `run_queued` checks the deployment is still QUEUED before converging;
`MaintenanceService.run` is idempotent by construction. That is why this layer
does not attempt exactly-once, which on a single database with no distributed
transaction it could not honestly offer.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from blitzecdn.capabilities.jobs.domain import (
    DEFAULT_LEASE_SECONDS,
    Job,
    JobClaim,
    JobStatus,
    backoff_for,
    utcnow,
)
from blitzecdn.capabilities.jobs.ports import JobHandler, JobStore

__all__ = ["DEPLOYMENT_JOB", "SCHEDULED_JOB", "JobQueue"]

_LOGGER = logging.getLogger(__name__)

#: The two kinds of work this control plane queues, and the payload key each
#: carries. Named constants because they are a durable wire contract between
#: the process that enqueues and the process that runs — a row written by
#: yesterday's controller is read by today's worker.
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
        clock: Any = utcnow,
    ) -> None:
        self.jobs = jobs
        #: Kind to handler. The worker resolves a claimed job through this and
        #: learns nothing else about it, which is what lets a convergence and a
        #: plugin-contributed maintenance run share one loop.
        self.handlers = dict(handlers)
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.retention = retention
        #: Injected so a test can move time without sleeping. Everything in
        #: this class reads it rather than calling `datetime.now` directly, so
        #: there is one clock and a test cannot half-freeze it.
        self.clock = clock

    # -- Publishing ----------------------------------------------------

    def enqueue_deployment(self, deployment_id: str) -> Job | None:
        """Queue a convergence for the worker.

        Deduplicated on the deployment id, so a second enqueue of a deployment
        already waiting is a no-op rather than a second run of the same row.
        The convergence path checks the deployment is still QUEUED anyway —
        this makes the common case cost nothing rather than cost a claim.
        """
        return self.jobs.enqueue(
            kind=DEPLOYMENT_JOB,
            payload={"deployment_id": deployment_id},
            available_at=self.clock(),
            dedupe_key=f"{DEPLOYMENT_JOB}:{deployment_id}",
        )

    def enqueue_scheduled(
        self, job: str, *, available_at: datetime | None = None, max_attempts: int = 1
    ) -> Job | None:
        """Queue one firing of a recurring job, at most one at a time.

        ``max_attempts`` of one, and deliberately. Recurring work retries by
        recurring: a renewal that failed will be attempted again at the next
        interval, against fresh state, and a queue-level retry minutes later
        would only add a second failure to the log for the same reason.
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
        """Claim one job, run it, and record what happened.

        Returns the job it ran, or ``None`` when the queue was empty. The
        return is the job as it was claimed rather than as it ended, because
        the caller that wants the outcome reads it back — and a value that
        pretended to be current would be current only until the next worker
        touched it.
        """
        claim = self.claim(lease_seconds=lease_seconds)
        if claim is None:
            return None
        self.run(claim)
        return claim.job

    def run(self, claim: JobClaim) -> None:
        """Run a claimed job under its fence, recording success or failure.

        A handler that raises is a failure; a kind with no handler is a failure
        too, and a permanent one — retrying it would produce the same missing
        handler at each attempt. That happens when a plugin is uninstalled
        while one of its jobs is still queued, which is why the message names
        the kind rather than only reporting a lookup error.
        """
        job = claim.job
        handler = self.handlers.get(job.kind)
        if handler is None:
            self._fail(
                claim,
                f"no handler is registered for job kind {job.kind!r}; the "
                "distribution that contributed it may have been uninstalled",
                permanent=True,
            )
            return
        try:
            handler(job.payload)
        except Exception as exc:
            _LOGGER.exception("job %s (%s) failed", job.id, job.kind)
            self._fail(claim, f"{type(exc).__name__}: {exc}", permanent=False)
            return
        if not self.jobs.complete(job.id, claim.fence, now=self.clock()):
            # The lease expired and somebody else took the job over while this
            # worker was inside the handler. The work was done twice, which the
            # handlers tolerate; what must not happen is this worker recording
            # an outcome for a run that is no longer its own.
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

    def _fail(self, claim: JobClaim, error: str, *, permanent: bool) -> None:
        now = self.clock()
        job = claim.job
        # `attempts` was already incremented by the claim, so this compares the
        # attempt that just failed rather than the one before it.
        retry_at = (
            None if permanent or job.exhausted else now + backoff_for(job.attempts)
        )
        self.jobs.fail(job.id, claim.fence, now=now, error=error, retry_at=retry_at)
