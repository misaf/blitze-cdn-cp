"""Edge operations queued for the worker, and reading back how they went.

An operation is a job of a declared kind run once: no automatic retry, since
the operator asked for one run and reads its outcome. Only declared kinds are
submitted or read, so deployment and scheduled jobs stay internal.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from blitzecdn.capabilities.jobs.domain import Job
from blitzecdn.capabilities.jobs.service.queue import JobQueue
from blitzecdn.core.exceptions import ConflictError, NotFoundError

__all__ = ["OperationService"]


class OperationService:
    """Submit an operation to the worker, and look one up by id."""

    def __init__(self, *, queue: JobQueue, kinds: Iterable[str]) -> None:
        self.queue = queue
        self.kinds = frozenset(kinds)

    def submit(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        dedupe_key: str | None = None,
    ) -> Job:
        """Queue one run of ``kind``; refuse a second while ``dedupe_key`` is busy."""
        if kind not in self.kinds:
            raise NotFoundError(f"no operation kind {kind!r} is declared")
        job = self.queue.jobs.enqueue(
            kind=kind,
            payload=payload,
            available_at=self.queue.clock(),
            dedupe_key=dedupe_key,
            max_attempts=1,
        )
        if job is None:
            raise ConflictError(
                f"a {kind} operation for the same target is already queued or "
                "running; wait for it to finish"
            )
        return job

    def get(self, operation_id: str) -> Job:
        job = self.queue.get(operation_id)
        if job.kind not in self.kinds:
            raise NotFoundError(f"operation does not exist: {operation_id}")
        return job
