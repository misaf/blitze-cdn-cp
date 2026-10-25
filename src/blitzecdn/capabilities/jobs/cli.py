"""Looking at the durable work queue from an operator's terminal.

These commands inspect jobs and schedules without creating or retrying work.
A recorded lease identifies the previous owner and its deadline, not proof that
its process is alive. Recovery remains the worker's responsibility.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

import typer

from blitzecdn.capabilities.jobs.domain import JobStatus, Schedule
from blitzecdn.entrypoints.cli import common

job_app = typer.Typer(help="Durable background work: what is queued and what ran.")


def _schedules() -> Sequence[Schedule]:
    """Every registered schedule, newest information first.

    A helper rather than the expression inline, because the expression reaches
    through two objects to get there and reads worse than the sentence it
    stands for.
    """
    return common.services().job_scheduler.schedules.list_schedules()


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
    After a worker stops, takeover waits for leased_until; restarting does not
    clear the lease. Use job show JOB_ID --json for the recorded deadline.
    """
    common.emit(
        [
            _row(job)
            for job in common.services().job_queue.list_jobs(status=status, limit=limit)
        ],
        json_output=json_output,
    )


@job_app.command("show")
def job_show(
    job_id: Annotated[str, typer.Argument(help="The job's identifier.")],
    json_output: common.JsonOutput = False,
) -> None:
    """One job, including its lease owner, deadline and last error.

    leased_by records the worker host/process; it does not prove liveness.
    If that worker stopped, a fresh worker must wait until leased_until before
    reclaiming the job. Deployment leases default to 900 seconds; scheduled
    jobs can differ. Inspect the recorded deadline rather than timing restart.

    After expiry, check worker health/logs and last_error if no progress occurs.
    Expiry permits a claim; it does not guarantee immediate or successful work.
    Do not edit queue rows or submit duplicate work to bypass the wait.
    """
    common.emit(_row(common.services().job_queue.get(job_id)), json_output=json_output)


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
