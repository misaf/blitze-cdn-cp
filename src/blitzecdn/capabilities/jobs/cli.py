"""Looking at the durable work queue from an operator's terminal.

Read-only, plus one deliberate exception. Nothing here enqueues: work is
created by the thing that needs it — a deploy, a schedule coming due — and a
command that put a job in the table by hand would be a second way to start a
convergence with none of the checks the first one does.

The exception is ``job retry``, which exists because the alternative is worse.
A job that exhausted its attempts is a row an operator has read, understood and
fixed the cause of; without this their only recourse is to redo whatever
created it, which for a failed deployment means a second deployment record for
one intended change.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

import typer

from blitzecdn.capabilities.jobs.domain import JobStatus, Schedule
from blitzecdn.cli import common

job_app = typer.Typer(help="Durable background work: what is queued and what ran.")


def _schedules() -> Sequence[Schedule]:
    """Every registered schedule, newest information first.

    A helper rather than the expression inline, because the expression reaches
    through two objects to get there and reads worse than the sentence it
    stands for.
    """
    return common.control_plane().job_scheduler.schedules.list_schedules()


def _row(job: object) -> dict[str, object]:
    from blitzecdn.capabilities.jobs.domain import Job

    if not isinstance(job, Job):  # pragma: no cover - defensive, never reached
        raise TypeError(f"expected a Job, got {type(job).__name__}")
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status.value,
        "attempts": f"{job.attempts}/{job.max_attempts}",
        "payload": job.payload,
        "available_at": job.available_at.isoformat(),
        "leased_by": job.leased_by,
        "leased_until": job.leased_until.isoformat() if job.leased_until else None,
        "last_error": job.last_error,
    }


@job_app.command("list")
def job_list(
    status: Annotated[
        JobStatus | None,
        typer.Option("--status", help="Only jobs in this state."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 50,
    json_output: common.JsonOutput = False,
) -> None:
    """Recent durable work, newest first.

    ``leased_by`` names the host and process holding a running job, which is
    what an operator needs in order to go and look at a run that is stuck.
    """
    common.emit(
        [
            _row(job)
            for job in common.control_plane().job_queue.list_jobs(
                status=status, limit=limit
            )
        ],
        json_output=json_output,
    )


@job_app.command("show")
def job_show(
    job_id: Annotated[str, typer.Argument(help="The job's identifier.")],
    json_output: common.JsonOutput = False,
) -> None:
    """One job, including the error that ended it."""
    common.emit(
        _row(common.control_plane().job_queue.get(job_id)), json_output=json_output
    )


@job_app.command("schedules")
def job_schedules(json_output: common.JsonOutput = False) -> None:
    """Recurring work, and when each piece of it is next due.

    Durable, so this is what will actually happen rather than what an
    in-process timer currently intends: a controller restarted an hour from now
    resumes these times instead of resetting them.
    """
    common.emit(
        [
            {
                "name": schedule.name,
                "every_seconds": schedule.interval_seconds,
                "lease_seconds": schedule.lease_seconds,
                "next_run_at": schedule.next_run_at.isoformat(),
                "last_run_at": (
                    schedule.last_run_at.isoformat() if schedule.last_run_at else None
                ),
            }
            for schedule in _schedules()
        ],
        json_output=json_output,
    )
