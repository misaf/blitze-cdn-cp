# 0001: SQLite for state and the job queue; no message broker

Status: Accepted

## Context

The control plane manages a fleet from one host. Its background work is
small: at most one deployment at a time, a handful of queued edge operations
(purges, origin checks, certificate requests), and a few scheduled jobs such
as drift checks, traffic collection and backups. That work still has to
survive a restart, run only once at a time, and recover from a worker that
dies mid-job.

A broker such as Redis or RabbitMQ would add a second stateful service to
install, back up, secure and keep consistent with the database. A job and the
state change that queued it would also commit in two places.

## Decision

All state, including the job queue, lives in one SQLite database
(`core/persistence/engine.py`). A job is a row
(`capabilities/jobs/domain/job.py`), and durability, single-flight and
recovery are columns on it:

- `available_at` carries delay and backoff.
- A claim sets `leased_until`. The worker renews it with a heartbeat while the
  handler runs, so a dead worker's job becomes claimable again with nothing
  having to notice.
- `fence` increments on every claim, and finishing needs the claim's token, so
  a worker paused past its lease cannot complete a job someone else re-ran.

Workers poll (`capabilities/jobs/service/runner.py`). An idle poll is one
indexed `SELECT`.

## Consequences

- A job is enqueued in the same transaction as the state that asked for it.
  A deployment row, its targets and its job commit together, so a refused
  enqueue leaves nothing stranded.
- One file to back up and restore (`capabilities/backup`).
- Writes are serialized. A use case reserves the writer with
  `BEGIN IMMEDIATE` at its boundary, and reads outside a Unit of Work run
  beside it under WAL. Long work, such as rendering or running Ansible,
  happens outside write transactions. See [transactions](../transactions.md).
- Delivery is at least once. Handlers tolerate redelivery, and queued edge
  operations run with one attempt.
- The API, the worker and the CLI must share one state directory on one host.
  Horizontal scaling of the control plane is not supported.
- Convergence is serialized by a separate advisory file lock
  (`core/runtime/lock.py`), because the API, CLI and worker are separate
  processes and an Ansible run must not hold a database transaction open.
