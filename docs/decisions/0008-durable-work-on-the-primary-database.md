# Durable background work belongs on the database we already have

Status: accepted; documents the design in `capabilities.jobs` and the removal
of `core.runtime.broker`, `composition.scheduler` and the Dramatiq worker.

## Context

Background work had four moving parts, three of them outside the database that
already held every durable fact about an installation.

A **Redis broker** carried messages. **Dramatiq actors** in a separate worker
process consumed them, started by the `dramatiq` CLI — so the process the
control plane ran was one this project did not define and could not hand a
signal handler to. An **APScheduler timer** inside the API process decided when
recurring work fired. And a **Redis key with a TTL** stood in for "only one copy
of this scheduled job at a time".

Each of those was defensible on its own. Together they cost a standalone
installation a second network service to run, supervise, back up and firewall,
and they left three defects that are worth naming because they are what
actually decided this.

**A missed firing was invisible.** The schedule lived in memory. A controller
that was down over the hour a certificate renewal was due came back with the
timer reset and simply never ran that firing; the job's next chance was a whole
interval later, and nothing anywhere recorded that one had been missed.

**The single-flight key could outlive its work.** `SET NX` with a TTL is a lock
held by a process, released by that process. A worker that died holding one left
the key in place for the rest of its TTL — computed as twice the job's interval
— so a crash during a renewal blocked the next renewal too.

**A paused worker could still finish its job.** Dramatiq's redelivery makes a
message another worker's after a timeout, but nothing stopped the first worker
from coming back and reporting success for a run that had already been redone
elsewhere. A lease says who *may* work; only a fence says whose result *counts*.

Against all that: the workload is one convergence at a time — the fleet-wide
deployment lock says so — and a handful of recurring jobs on intervals measured
in hours.

## Decision

**A job is a row in the control plane's own database**, and three columns carry
the whole of the guarantee.

`available_at` is when a job may next be claimed: the initial delay, the backoff
after a failure, and nothing else.

`leased_until` is a claim's expiry. A worker that dies stops renewing and the
lease runs out; nothing has to observe the death for the work to become
claimable, and crash recovery is therefore not a sweep or a startup pass but the
same query that finds any other runnable job.

`fence` increments on every claim, and finishing a job requires the token the
claim returned. A worker paused past its lease — a long stop-the-world pause, a
suspended container, a partition it never noticed — comes back holding a stale
token and its write matches no row. That is the part a lease alone does not give
you, and it is why "at least once" is safe to build on here.

**Single flight is a partial unique index**, not a lock: `dedupe_key` is unique
across jobs that are pending or running, and it is cleared when a job finishes.
It cannot outlive the work it guards, because it *is* a column on that work.

**The schedule is a table.** `next_run_at` is durable, so a schedule that came
due during an outage is still due when the controller returns. Two controllers
ticking at the same instant both see it; the compare-and-swap on `next_run_at`
means exactly one moves it and only that one enqueues. An overdue schedule fires
once rather than once per missed interval — the work is "renew what is
expiring", not "produce a report for each hour".

**Delivery is at least once, and the handlers were built for it.** `run_queued`
returns the deployment untouched unless it is still QUEUED; a maintenance run is
idempotent. Exactly-once is not offered because on one database with no
distributed transaction it could not be offered honestly, and a layer that
claimed it would be a layer people trusted.

**The worker polls.** An idle tick is one indexed SELECT against a local file.
Push needs something to push through, and that something was the service this
record removes.

## Alternatives rejected

*Keep Redis, fix the three defects.* Fencing and a durable schedule are the
fixes, and both want a transactional store. Having built them, the broker
carries nothing the table does not — while still being a service to run, patch
and back up.

*PostgreSQL, with `SKIP LOCKED`.* The natural home for a job queue at scale, and
genuinely better at contention. It is also a database to install, tune, back up,
restore and upgrade on a single-node standalone installation whose entire state
is one SQLite file — for a workload of one convergence at a time. SQLite's
`BEGIN IMMEDIATE` serialises writers, which is all the claim actually needs;
`SKIP LOCKED` would matter with many workers competing, and there is one.
Revisit this if the control plane ever becomes multi-node, at which point the
store changes and none of the domain above it does.

*Cron expressions for the schedule.* Every recurring job here is an interval and
none cares what time of day it runs. A cron expression would buy a parser, a
timezone question and a class of "why did this not fire" bugs for precision
nothing has asked for.

## Consequences

A standalone installation is one service lighter: the Compose project went from
four services, a named volume and a `depends_on` from every service to three
services and no volume. `install.sh uninstall` has one fewer thing to remove.

`blitzecdn.worker` is a process this project defines. It installs its own
SIGTERM handler, so a container stop finishes the job in hand instead of being
killed mid-convergence and waiting out a lease.

`blitzecdn job list` and `blitzecdn job schedules` show what is queued, what is
running and when recurring work is next due — none of which was answerable from
outside Redis before.

`BLITZE_REDIS_URL` and the `redis_url` setting are gone. An installation that
sets either is not broken by it: unknown keys in `blitzecdn.toml` and unknown
`BLITZE_*` names were already ignored rather than refused. Operators should
remove them, and a Redis container left behind by an older installation can be
stopped and deleted — nothing reads it.

Three dependencies left the lockfile: `redis`, `dramatiq` and `apscheduler`.

## Code and verification

- [The job, and its lifecycle rules](../../src/blitzecdn/capabilities/jobs/domain/job.py)
- [The schedule](../../src/blitzecdn/capabilities/jobs/domain/schedule.py)
- [The queue](../../src/blitzecdn/capabilities/jobs/service/queue.py)
- [The scheduler](../../src/blitzecdn/capabilities/jobs/service/scheduling.py)
- [The worker loop](../../src/blitzecdn/capabilities/jobs/service/runner.py)
- [Conditional writes and the claim](../../src/blitzecdn/capabilities/jobs/adapters/persistence.py)
- [The worker entry point](../../src/blitzecdn/worker.py)
- [Leases, fences, retries and recovery](../../tests/capabilities/jobs/test_job_queue.py)
- [Durable scheduling](../../tests/capabilities/jobs/test_scheduling.py)
- [The Compose project](../../src/blitzecdn/ansible/roles/blitzecdn_controlplane/templates/compose.yml.j2)
