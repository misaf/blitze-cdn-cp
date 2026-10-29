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
* **Backups:** the last full backup's completion time, age, archive name and
  size, or all `null` when none is recorded. See [Backups](#backups).

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
| `blitzecdn_backup_last_success_timestamp_seconds` | Completion time of the last full backup, or zero when none is recorded |
| `blitzecdn_backup_last_success_size_bytes` | Size of that archive, or zero when none is recorded |

These metrics are **gauges**. History pruning can reduce job/workflow counts and
change average duration; do not apply `rate()` as if they were lifetime counters.
Labels contain only fixed statuses, never job IDs, hostnames, site names or
payloads. Detailed worker identifiers are available through authenticated status
reporting; certificate details remain at `GET /v3/certificates`.

Useful alert conditions include ready work with zero fresh workers, increasing
oldest-ready age, expired leases, stale/unreadable worker records, workflows
requiring review, expiring certificates, and an old or missing backup
(`time() - blitzecdn_backup_last_success_timestamp_seconds > 2 * 86400` fires
after two missed daily runs, and immediately on a controller that has never
recorded one). Choose durations and severity from
your operational recovery requirements. `/health` retains its existing database
and queue readiness checks; historical failures do not make it fail.

## Installing, updating and removing a server

A server is installed from one file. Fetch `install.sh` for the release you
want and run its standalone command; it installs Docker Engine, pulls that
release's control-plane image, and runs everything else from the image. No
source tree, virtualenv or Git checkout is left on the host.

```sh
curl -fsSLO https://raw.githubusercontent.com/misaf/blitze-cdn-cp/vX.Y.Z/install.sh
sudo bash install.sh standalone --admin-cidr 203.0.113.8/32 --email ops@example.com
```

The installation leaves that release's installer on the host as
`blitzecdn-install`, and every later operation runs it:

```sh
sudo blitzecdn-install update           # newest release of the installed line
sudo blitzecdn-install update --major   # next major line, once on the newest of this one
sudo blitzecdn-install standalone ...   # repair the installed release
sudo blitzecdn-install --uninstall      # remove everything but the backups
```

The image tag is the release. `update` reads the installed tag, lists the
published tags of `ghcr.io/misaf/blitzecdn-control-plane`, and moves to the
newest one in the same major line, never backwards and never across a major;
`--major` crosses one line, takes a full backup instead of the database alone,
and asks for the target version to be typed. Both refuse to run without a
backup unless given `--no-backup`. Set `BLITZECDN_CONTROL_PLANE_IMAGE` to a
tagged reference to name the target instead, for example from a mirror; it is
still refused unless it moves the way the command does.

A `standalone` run converges the release its installer names, so it refuses a
host that runs a different one. To start again from a clean state, uninstall,
then run the standalone command of the release you want.

## Backups

An installed controller takes a full backup every day and then prunes the
backup directory, `/var/backups/blitzecdn`, to the newest seven archives. A
systemd timer on the host, `blitzecdn-backup.timer`, runs
`blitzecdn backup create` and then `blitzecdn backup prune` through the CLI
wrapper. The prune runs only if the backup succeeded, so failed runs never
thin out good archives. It runs on the host rather than in the worker because
the wrapper is the only thing that can put the root-only
`/etc/blitzecdn/blitzecdn.env`, which holds the API keys and other secrets,
into the archive.

```sh
systemctl list-timers blitzecdn-backup.timer   # next and last run
journalctl -u blitzecdn-backup.service         # what the last runs printed
sudo systemctl start blitzecdn-backup.service  # take one now
```

The installer's role variables set the schedule and retention:

| Variable | Default | Meaning |
| --- | --- | --- |
| `blitzecdn_controlplane_backup_schedule` | `daily` | systemd `OnCalendar` expression; empty stops and disables the timer |
| `blitzecdn_controlplane_backup_retain_count` | `7` | Rendered as `backup_retain_count`; `0` keeps every archive |

A missed run, for example while the host was off, happens soon after it comes
back. Each run is delayed by up to 15 minutes at random.

Retention counts and removes only archives named the way `backup create` names
them (`blitzecdn-backup-YYYY-MM-DD_HH-MM-SSZ.tar.gz`). Other files in the
directory, and archives written elsewhere with `--output`, are never touched.
Run `blitzecdn backup prune --keep N` to apply a different count once.

Every full backup, scheduled or manual, including the one
`blitzecdn-install update --major` takes, records itself in `backup-status.json` in the state directory. `overview`,
`GET /v3/status` and `/metrics` report from that file, so they keep working
without the backup directory and without the database. A `--only` backup is not
recorded, including the database-only one `blitzecdn-install update` takes: it
cannot rebuild a controller.

**Copy archives off the host.** A backup that exists only on the disk it
protects is lost with that disk. Each archive holds every private key, the
controller's SSH identity, the secrets file and the full audit trail, so keep
the copy encrypted and access-controlled. For example, from another machine:

```sh
rsync -a --chmod=F600 root@controller:/var/backups/blitzecdn/ ./blitzecdn-backups/
```

Or point an encrypted backup tool such as restic or borg at
`/var/backups/blitzecdn` on the same schedule. Uninstalling stops and removes
the timer but leaves the backup directory in place.

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

## Hostname access and error logs

Each edge stores separate persistent logs for every configured hostname:

```text
/var/log/nginx/domains/example.com.access.log
/var/log/nginx/domains/example.com.error.log
/var/log/nginx/domains/api.example.com.access.log
/var/log/nginx/domains/api.example.com.error.log
```

HTTP and HTTPS requests on all supported ports use the same pair of files for
that hostname. Hostnames sharing an origin or policy still have separate files.
The release renderer emits one server block per hostname to isolate Nginx error
logs as well as access logs. These files live in the edge's writable log mount,
outside configuration releases, and survive reloads, rollbacks and container
replacement. A custom `blitzecdn_edge_runtime.paths.logs` changes the root.

Wildcard configurations have one pair for the configured pattern:
`*.example.com` uses `_wildcard.example.com.access.log` and
`_wildcard.example.com.error.log`. All hostnames matched by that wildcard share
those files; the access records still contain the actual requested host.
Individual error files for arbitrary wildcard matches require a separate log
collector. Explicit hostnames matching a wildcard use their own server blocks
and log files.

Hostname access files use JSON Lines: one object per logged request, with
`schema_version: 1`, a generated `request_id`, an ISO 8601 `timestamp` with
timezone, the actual requested `hostname`, and the configured `server_name`
(which may be a wildcard).
`bytes_sent`, `body_bytes_sent` and `status` are JSON integers;
`request_time` is a JSON number in seconds. Diagnostic fields include client IP,
request line, cache status, upstream address and response time, referer and user
agent. String fields use Nginx JSON escaping; unavailable upstream values may be
`"-"`, and an absent cache header may be empty. Error logs retain Nginx's native
text format.

The visitor bandwidth counter sums `bytes_sent`: bytes Nginx reports
sending to the client, including response headers. `body_bytes_sent` excludes
response headers. These are application traffic measurements, not interface
counters or a provider's billable transfer total. Request counts count log
records, including redirects and errors. A long-running response is recorded
when its request finishes, so its traffic is attributed to that completion
time. See [Nginx's variable definitions](https://nginx.org/en/docs/http/ngx_http_core_module.html#variables).

The [traffic capability](traffic-accounting.md) collects these records and
persists hourly totals. Scheduling and API/CLI access are separate steps.
Disabling access logging means there are no records to collect. Identity probes
suppressed by `access_log off` also remain excluded.
The existing aggregate `blitzecdn-access.log` keeps its text format for cache
diagnostics, and requests also reach Docker stdout. Hostname errors at `warn` and above go
to their own file and Docker stderr; startup errors or errors before Nginx has
selected a hostname can still appear in the global `error.log`.
`blitzecdn_nginx_access_log_enabled=false` disables access logging for managed
hostnames, while error logging remains enabled. Release identity probes continue
to suppress access logging.

The Nginx role installs `logrotate`, enables its host timer, and manages
`/etc/logrotate.d/blitzecdn-domains`. Hostname logs rotate daily, or when larger
than 100 MiB at a scheduled check, with up to 14 archives per file. Older archives
are compressed; compression of the newest archive is delayed. Rotation renames
the file and asks the running container's Nginx master to reopen logs, avoiding
`copytruncate` and an Nginx restart. The size threshold is checked when logrotate
runs, so it is not a hard disk quota. This policy covers hostname files; it does
not change retention for aggregate logs or Docker streams.

The hostname log directory is root-owned with group `adm` and mode `0751`, so
Nginx workers can traverse it when reopening files. Log files use mode `0640`,
owned by the runtime worker UID with group `adm`; other users cannot read them.
Removing a hostname leaves its logs intact. Edge teardown also retains logs by
default and removes the rotation policy; explicitly requesting
`blitzecdn_edge_teardown_remove_logs=true` removes hostname logs and their
archives as well. When overriding the runtime log root, set
`blitzecdn_edge_teardown_domain_logs_path` to the same root with `/domains`
appended before teardown. Logs remain local to each edge; this step adds no log
retrieval API or cross-edge statistics.
