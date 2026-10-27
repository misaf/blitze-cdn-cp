# Operational visibility

The current API uses `/v3`. Legacy production APIs `/v1` and `/v2` are
different contracts and are not served by this application. The API version
is independent of the BlitzeCDN application version.

Read local controller state with:

```sh
blitzecdn overview
blitzecdn overview --json
```

The authenticated `GET /v3/status` endpoint returns the same report. Supply the
existing `X-API-Key` header. Both interfaces read local state; they run no remote
probes and start no workers.

## What the report means

* **Queue:** counts over all retained jobs, ready jobs, oldest ready age, and
  expired running-job leases. Ready age starts at `available_at`, so an intentionally
  delayed job does not create a backlog alert before it is due.
* **Workers:** worker identity, heartbeat age, current job ID/kind, current job
  age, and last execution duration. Idle workers are reported too.
* **Workflows:** retained counts by status, including failures and `needs_review`,
  plus completed count and elapsed-duration sum. The latter include succeeded
  and failed workflows, excluding pending/running/interrupted work.
* **Certificates:** managed, expired, expiring within 30 whole days (including
  expired), due for ACME renewal, and earliest expiry. Uploaded certificates
  count toward expiry alerts but cannot be renewed automatically.

Workers publish a heartbeat every five seconds under `.state/workers/`, shared
with the API through the controller's state volume. A heartbeat older than 30
seconds is stale. An independent reporting thread continues pulsing while a
handler blocks. This establishes reporter liveness, **not remote progress or job
lease renewal**; use job age, lease expiry and deployment checkpoints together.
Graceful shutdown removes the worker's record. Startup removes crash records
older than one day. Corrupt or unreadable records are counted explicitly.

Worker records contain identifiers and timing only, with mode `0600`. They are
ephemeral and are not part of application backups. No schema migration is needed.

## Prometheus

The existing authenticated `GET /metrics` endpoint retains its edge, site and
deployment metrics and adds:

| Metric | Meaning |
| --- | --- |
| `blitzecdn_jobs{status}` | All retained jobs by status |
| `blitzecdn_jobs_ready` | Jobs eligible to run now |
| `blitzecdn_jobs_oldest_ready_age_seconds` | Oldest eligible job's wait |
| `blitzecdn_jobs_expired_leases` | Running jobs eligible for lease recovery |
| `blitzecdn_workers{state}` | Idle, busy and stale worker counts |
| `blitzecdn_worker_records_unreadable` | Damaged/inaccessible worker observations |
| `blitzecdn_worker_heartbeat_oldest_age_seconds` | Oldest observed heartbeat age |
| `blitzecdn_worker_current_job_oldest_age_seconds` | Oldest observed active job age |
| `blitzecdn_worker_last_job_duration_seconds` | Largest last execution duration among fresh workers |
| `blitzecdn_workflows{status}` | Retained workflow counts, including `needs_review` |
| `blitzecdn_workflow_completed_duration_seconds_average` | Mean duration of retained completed workflows |
| `blitzecdn_certificates_managed` | Managed certificate count |
| `blitzecdn_certificates_expired` | Expired certificate count |
| `blitzecdn_certificates_expiring` | Certificates with at most 30 whole days remaining |
| `blitzecdn_certificates_renewal_due` | ACME certificates due for renewal |
| `blitzecdn_certificate_next_expiry_timestamp_seconds` | Earliest expiry, or zero with no managed certificates |

These metrics are **gauges**. History pruning can reduce job/workflow counts and
change average duration; do not apply `rate()` as if they were lifetime counters.
Labels contain only fixed statuses, never job IDs, hostnames, site names or
payloads. Detailed worker identifiers are available through authenticated status
reporting; certificate details remain at `GET /v3/certificates`.

Useful alert conditions include ready work with zero fresh workers, increasing
oldest-ready age, expired leases, stale/unreadable worker records, workflows
requiring review, and expiring certificates. Choose durations and severity from
your operational recovery requirements. `/health` retains its existing database
and queue readiness checks; historical failures do not make it fail.

## Logs

The worker entrypoint emits JSON logs. Handler start/finish records include
`job_id`, `job_kind`, `worker_id`, `attempt` and `fence`; finish records include
`duration_seconds`. These time handler invocation rather than queue waiting.
Payloads and result bodies are not logged. A finished handler record does not
itself claim that the job succeeded; inspect its durable outcome.

Interrupted workflows moved to `needs_review` produce warnings with
`workflow_id`, `workflow_kind`, `resource_id` and the last checkpoint name.
Checkpoint details and credentials are omitted. Query `GET /v3/workflows` or the
workflow's existing detail endpoint to inspect its retained journal.

## HTTP traces and request metrics

The API uses [FastAPI's native OpenTelemetry support](https://fastapi.tiangolo.com/advanced/opentelemetry/).
The installed application includes its OpenTelemetry SDK and HTTP/protobuf
exporter. To send request telemetry to your collector, set these variables in the
API process environment before starting it:

```sh
export FASTAPI_OTEL_AUTO_CONFIGURE=true
export OTEL_SERVICE_NAME=blitzecdn-api
export OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.example.com
python -m blitzecdn.entrypoints.api
```

Use your collector's base URL; FastAPI appends `/v1/traces` and `/v1/metrics`.
If authentication is required, set `OTEL_EXPORTER_OTLP_HEADERS` to the collector's
header configuration. For the installed Compose services, place these variables
in `/etc/blitzecdn/blitzecdn.env` and recreate the API container. For local
development, export them in the shell: the application's `.env` reader only
loads application settings and does not populate the process environment.

Export is off unless explicitly enabled. If another monitoring library configures
providers and exporters, leave automatic setup disabled to avoid duplicate export.
Existing configured providers are used by FastAPI automatically.

Request spans include dependency resolution, endpoint execution and serialization.
Request metrics measure traffic, duration and active requests. `/health` and
`/metrics` are excluded to keep periodic probes out of these measurements.
Exception log export is disabled because exception messages can contain
infrastructure credentials; existing application logs remain local. Standard
HTTP span attributes include request paths and query strings, so collector access
and retention should reflect that data.

These traces cover API request processing. Queued work executes in a separate
worker and does not inherit the submitting request's trace context. Use the
operation/deployment ID to follow its durable outcome and worker logs. The
authenticated `/metrics` endpoint continues reporting operational state separately.
