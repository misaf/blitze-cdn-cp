"""Edge operations queued for the worker, and reading back how they went.

An operation is a job of a declared kind run once: no automatic retry, since
the operator asked for one run and reads its outcome. Only declared kinds are
submitted or read, so deployment and scheduled jobs stay internal.
"""

from __future__ import annotations

from collections.abc import Iterable

from blitzecdn.capabilities.jobs.domain import Job, JobPayload
from blitzecdn.capabilities.jobs.service.queue import JobQueue
from blitzecdn.core.exceptions import ConflictError, NotFoundError
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.idempotency import bind_claimed_key

__all__ = ["OperationService"]


class OperationService:
    """Submit an operation to the worker, and look one up by id."""

    def __init__(
        self, *, queue: JobQueue, kinds: Iterable[str], uow: UnitOfWork
    ) -> None:
        self.queue = queue
        self.kinds = frozenset(kinds)
        self.uow = uow

    def submit(self, operation: JobPayload, *, dedupe_key: str | None = None) -> Job:
        """Queue one run; refuse a second while ``dedupe_key`` is busy.

        The request's ``Idempotency-Key``, if it sent one, is bound in the same
        transaction as the job.
        """
        kind = operation.kind
        if kind not in self.kinds:
            raise NotFoundError(f"no operation kind {kind!r} is declared")
        with self.uow.transaction():
            job = self.queue.enqueue(operation, dedupe_key=dedupe_key, max_attempts=1)
            if job is None:
                raise ConflictError(
                    f"a {kind} operation for the same target is already queued or "
                    "running; wait for it to finish"
                )
            bind_claimed_key(job.id)
        return job

    def get(self, operation_id: str) -> Job:
        """One queued edge operation, for a client polling its outcome.

        Raises ``NotFoundError`` for an unknown id or a job that is not an operation.
        """
        job = self.queue.get(operation_id)
        if job.kind not in self.kinds:
            raise NotFoundError(f"operation does not exist: {operation_id}")
        return job
