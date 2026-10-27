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
configuration does not erase historical usage. This step does not prune totals
or cursors. Deleting cursors independently of totals can cause a retained log
to be counted again; backups must retain both tables together.

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
Use `job schedules`, worker logs and queue status to investigate freshness.
These are Nginx bytes sent to visitors: `bytes_sent` includes HTTP headers,
while `body_bytes_sent` excludes them. They do not measure origin traffic or
provider billing. Requests are assigned to their completion hour.

Pagination reads current saved data; collecting more traffic between requests
can change rows and offsets. Query a completed range after collection catches
up when exporting multiple pages. This interface does not provide a frozen
export snapshot or billing reconciliation.
