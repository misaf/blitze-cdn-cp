# Visitor traffic accounting

The traffic capability collects hourly request counts, bytes sent to visitors,
and response body bytes per edge and actual requested hostname. Hours use UTC
and the log timestamp at request completion. Redirects and errors count as
requests. Release identity probes remain excluded by Nginx's `access_log off`.
Counter semantics and the JSON format are described in [operations](operations.md#hostname-access-and-error-logs).

The traffic service and scheduled worker job collect and persist these totals.
Traffic API routes and CLI commands are not exposed yet.

## Collection and commits

The packaged Ansible play runs a standalone standard-library Python collector
on each edge. Raw URLs, headers and client addresses stay on the edge. Only
hourly aggregates, progress cursors and safe diagnostics return to the controller.
The controller performs network collection outside database transactions.
Successful batches for each edge, their progress, and the audit event commit in
one transaction. An unreachable edge or a damaged file does not hide successful
batches from other edges or files.

A stream is identified by the SHA-256 of its first complete record, including
Nginx's generated 128-bit `request_id`. The identity survives rename and gzip
compression and is independent of reusable filesystem inodes. A checkpoint
records the uncompressed byte offset and a hash of the last 64 bytes preceding
it. Changed or truncated checkpoint data is refused; it is never silently reset.
These rules require the current JSON format with generated request IDs and the
managed rename/reopen rotation policy. Legacy text logs and JSON records without
request IDs are reported as unusable, rather than guessed at or counted twice.

The collector reads complete newline-terminated records from active files,
uncompressed archives and gzip archives. Partial trailing records stay pending.
A corrupt or oversized record blocks its file without advancing that file's
cursor; other valid files can still progress. The maximum record size is 64 KiB.
Each invocation processes at most 4 MiB of new record bytes and 128 stream batches per
edge by default; `more` reports remaining work. Completed streams do not emit
empty batches. Gzip seeking may decompress already processed bytes, so the batch
bound limits new data, not total disk I/O. Uncompressed archives are cheaper to
read during normal frequent collection.

## Persistence and failure handling

`traffic_hours` uses `(edge, hostname, hour)` as its primary key.
`traffic_cursors` uses `(edge, stream_id)` and advances with the matching totals.
A retried or already committed batch adds nothing. An overlapping stale batch
is refused and must be recollected from current checkpoints. SQLite reserves
the writer before checking progress, including across independent processes.
An interrupted transaction, counter overflow or failed audit write rolls back
both the increments and progress. Counters use signed 64-bit integers.

Traffic rows do not depend on live domain or edge foreign keys, so removing
configuration does not erase historical usage. Retention expires it according
to the rules below. Manually deleting cursors can cause retained traffic to be
counted again; backups must retain all traffic tables together.

Collection is not a guarantee against lost source logs. Deleted logs, expired
archives, a restored database older than the available logs, or an edge disk
failure can leave uncollectable traffic. Collection must run frequently enough
to stay within the log retention window. Ordinary append, rename and gzip
rotation are supported; external edits and `copytruncate` are not. Byte totals
are Nginx application traffic measurements, not billing-grade network counters.

For a custom log root, set `blitzecdn_traffic_collect_log_directory` to that
root with `/domains` appended. The collector never modifies log files or
keeps edge-local state. Its temporary script is removed even when execution
fails. Persistent progress belongs to the controller database.

## Scheduled collection

The `collect-traffic` job uses the existing SQLite queue and worker. It runs
once every 60 seconds by default. API and worker startup register the schedule;
the first firing is due one interval later. An empty fleet is a successful
no-op. Collection requires a running worker; the API process does not collect
logs itself.

```dotenv
BLITZE_TRAFFIC_COLLECTION_INTERVAL_SECONDS=60
BLITZE_TRAFFIC_COLLECTION_MAX_PASSES=3
```

The equivalent `[blitzecdn]` TOML fields are
`traffic_collection_interval_seconds` and `traffic_collection_max_passes`.
The interval supports 0–86400 seconds; zero removes the recurring schedule on
restart. It does not cancel work already queued or running. The pass limit
supports 1–10. These are typed traffic capability settings, available through
existing configuration introspection.

Each pass retains the collector's 4 MiB/128-stream limit per edge. Subsequent
passes target only edges reporting remaining data, at least one newly committed
request, and no errors. A partial trailing line or duplicate delivery cannot
cause an immediate retry loop. At most three fleet calls run by default;
remaining work waits for the next interval. If the explicit follow-up target
list exceeds the shared fleet limit, it also waits for the next interval rather
than broadening collection to unrelated edges.

The queue deduplicates unfinished `collect-traffic` jobs. Its declared lease
covers the pass limit times the configured playbook timeout plus 60 seconds,
or twice the collection interval, whichever is greater. Due firings skip an
already unfinished job instead of accumulating a backlog of duplicate jobs.

Worker logs record per-edge counts, remaining-work flags and safe errors for
each pass, with a warning when backlog remains. An edge error fails the job
with the affected names. Healthy edges' committed totals remain intact.
Recurring jobs get one attempt per firing; the next scheduled run retries from
fresh database checkpoints. Use the read-only interfaces below to inspect saved counters.


## Reading usage

Authenticated `GET /v3/traffic/usage` and `blitzecdn traffic usage` return saved
hourly rows per served hostname and edge. They never contact edges or enqueue
collection. Historical rows remain queryable after removing an edge or domain.

Both require `start` (inclusive) and `end` (exclusive), in ISO 8601 with a timezone.
Bounds must resolve to exact UTC hours, and end must follow start. For example,
`2026-10-27T12:30:00+03:30` is the UTC hour `2026-10-27T09:00:00Z`.
Optional `hostname` and `edge` filters match exact names; hostnames are normalized
using the normal hostname validation. Hostname wildcards are not query filters.

```sh
blitzecdn traffic usage \
  --start 2026-10-27T00:00:00Z --end 2026-10-28T00:00:00Z \
  --hostname api.example.com --limit 100 --offset 0 --json
```

For HTTP, pass the same fields as query parameters to `/v3/traffic/usage`.
Both interfaces return `{items, total, limit, offset}`. `total` counts matching
hourly rows, not requests or bytes. Limit defaults to 100 (1–500); offset starts
at zero. Rows are ordered by hour ascending, then hostname and edge, with a
range index supporting the read. Each item contains `edge`, `hostname`, `hour`
(UTC), `requests`, `bytes_sent` and `body_bytes_sent` as integer counters.
No totals across rows or edges are implied by the page envelope.

Empty hours are omitted. An empty page does not prove zero traffic: collection
may be delayed, disabled, or failing, and access logging may be disabled.
Use the collection status below, plus `job schedules`, worker logs and queue
status, to investigate freshness.
These are Nginx bytes sent to visitors: `bytes_sent` includes HTTP headers,
while `body_bytes_sent` excludes them. They do not measure origin traffic or
provider billing. Requests are assigned to their completion hour.

Pagination reads current saved data; collecting more traffic between requests
can change rows and offsets. Query a completed range after collection catches
up when exporting multiple pages. This interface does not provide a frozen
export snapshot or billing reconciliation.


## Retention

`BLITZE_TRAFFIC_RETENTION_DAYS=90` keeps 90 days of hourly visitor traffic by
default. The equivalent `[blitzecdn]` TOML field is `traffic_retention_days`.
Values are 0–3650; zero disables future expiry. Restart processes after changing
configuration. Increasing retention or disabling expiry cannot restore data
already discarded or reopen the persisted expiry cutoff.

The independent `prune-traffic` worker job runs hourly, including when collection
is disabled or no edges are registered. Each run advances a persisted cutoff to
`now - retention_days`, rounded down to a UTC hour, and deletes at most 10,000
older hourly rows. The cutoff never moves backward, including after a clock
rollback. The hour at the cutoff is retained. Queries immediately exclude older
rows even if physical deletion needs subsequent runs. A backlog produces a worker
warning and continues next hour. The job records `traffic.pruned` in the audit
journal; deletion and the cutoff commit together, or both roll back.

Collection still advances through expired records, but never adds their counts
back. Each stream checkpoint tracks its latest covered hour, preserving the
maximum even if request timestamps arrive out of order. Once every hour that a
checkpoint covers has expired, a complete, error-free edge scan may remove that
checkpoint if its stream is absent. At most 128 missing streams are reported per
scan, oldest checkpoints first. Existing files keep their checkpoints, including
quiet active files; archives renamed or compressed retain the same stream ID.

Failed, truncated, partial, or bounded scans never authorize checkpoint deletion.
Offline, removed or perpetually backlogged edges retain checkpoints until a clean
scan can confirm absence. This conservative rule can leave old checkpoints, but
prevents forgetting progress on a file whose retained traffic could be replayed.
Restoring an expired stream after its checkpoint was removed cannot resurrect
expired totals: the persisted cutoff rejects its old buckets. New, retained hours
from the same stream are still collected.

Retention changes controller data only; edge logrotate keeps its existing policy.
It does not prune queue or audit history. Deleted SQLite pages become reusable;
this job does not run blocking `VACUUM` or promise an immediate file-size decrease.
Back up `traffic_hours`, `traffic_cursors`, `traffic_retention`, and
`traffic_collections` together. As
with any older database restore, traffic lost between the backup and available
edge logs cannot be reconstructed automatically.


## Collection freshness

Authenticated `GET /v3/traffic/status/{edge}` and
`blitzecdn traffic status EDGE --json` inspect persisted collection observations.
Reading status never contacts an edge, starts work, changes counters or adds audit
history. HTTP responses use `Cache-Control: no-store`. Registered edges without an
attempt return `never_collected`. Removed edges with history remain queryable and
show `registered=false`; unknown edges without history return 404.

Both interfaces return the same fields:

- `registered`, `collection_enabled`, `interval_seconds`: current configuration;
  collection is enabled for an edge when it is registered and the interval is nonzero.
- `checked_at`, `stale_after_seconds`, `stale`: the current check time and the age
  test. The threshold is twice the configured interval, with a minimum of 60 seconds.
  `stale` is true when no caught-up observation exists or its age reaches that threshold.
- `last_attempt_started_at`, `last_attempt_finished_at`: the latest attempt, recorded
  before remote work begins and when its result is committed. A missing finish time
  means no completion was recorded; it does not prove a worker is still alive.
- `last_success_at`: completion time of the latest error-free scan, including scans
  with backlog. Successful scans with no new requests also update this timestamp.
- `last_caught_up_at`: start time of the latest error-free scan reporting no backlog.
  Using its start avoids overstating freshness after a long-running scan.
- `pending`: whether the latest completed, error-free scan reported remaining data.
  It is null before completion or on failure, because backlog is then unknown.
- `last_error`: a generic diagnostic on failure. Raw log lines, remote output and
  exception details are not published here; inspect worker logs and job status.

`state` is one of:

| State | Meaning |
| --- | --- |
| `disabled` | Scheduling is disabled or the edge is no longer registered. |
| `never_collected` | No attempt has been recorded for this registered edge. |
| `collecting` | A recent attempt has started without a recorded finish. |
| `failed` | The latest completed attempt failed or returned partial errors. |
| `backlogged` | An error-free scan still reports remaining records. |
| `stale` | The latest caught-up scan is too old, or an unfinished attempt has aged past the threshold. |
| `current` | A recent, completed, error-free scan reported no backlog. |

These states take precedence in that order, except unfinished attempts are
`collecting` or `stale` according to their own age. `stale` remains an independent
age flag: a recent caught-up scan may still be within the threshold while a newer
attempt is collecting, failed, or backlogged. Previous success and caught-up times
are preserved during subsequent failures. A recovery clears `last_error`.

Each attempt has an internal identifier. Finishing an older overlapping attempt
cannot replace a newer observation. Successful status updates commit in the same
transaction as traffic increments, cursors and their audit event. An audit failure
rolls those changes back and records failure separately. Starting an attempt is
persisted before network work, so a stopped worker leaves an unfinished attempt
that becomes stale instead of appearing successful. Fleet failures mark the
selected edges; unreported targets are failures too. Status is one row per observed
edge and survives service restarts and configuration removal.

`current` describes collection of the logs available to that scan. It does not
prove zero traffic, enable disabled access logs, detect logs already deleted before
collection, prove complete historical coverage, or measure worker liveness. Status
is per edge, not per hostname: use it alongside the edge fields in usage rows. The
usage API and its pagination contract are unchanged. Deployment and live validation
of these interfaces remain a separate step.
