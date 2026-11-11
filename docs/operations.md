# Operational visibility

The current API uses `/v3`. The API version is independent of the BlitzeCDN
application version.

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
  count toward expiry alerts but cannot be renewed automatically. Also the
  number of sites awaiting a first certificate whose last preflight blocked
  (usually DNS not pointing at an edge yet), when one was last checked, and
  when the soonest is checked next. Scheduled reconciliation backs off such a
  site, doubling the wait from one reconcile interval up to a day, until the
  site, its DNS record or its deployment changes; `blitzecdn cert reconcile` and
  `POST /v3/certificates/reconcile` check every waiting site at once. After a
  blocking DNS failure, preflight skips the CAA lookup and the origin probe.
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

## Edge configuration retention

After a verified deployment commits on an edge, the configuration helper prunes
completed release trees under `/etc/blitzecdn/releases`. It keeps the active
release, its immediate predecessor, and two additional completed releases by
default. Set the Ansible variable `blitzecdn_config_release_keep_older` to a
nonnegative integer to change the extra count; zero still keeps the active
release and its predecessor. Older releases retained by the controller can be
staged again when an operator requests a rollback.

Pending transactions protect both their candidate and previous release. Staged
candidates and transactions whose rollback can still be replayed are also kept.
Completed journals are collected with their trees, so their old predecessor
references do not retain the entire history. All helper mutations share an
edge-local file lock. Check mode does not collect anything.

Collection runs only after the commit is durable. Filesystem failures or malformed
journals defer collection with a warning in the Ansible task's stderr; the
deployment stays committed and a later commit retries cleanup. Trees without a
transaction journal, including abandoned staging attempts, are preserved because
they cannot safely be distinguished from an in-progress stage. This retention
does not remove external TLS material, runtime data, logs, or container images.

## Controller deployment retention

Each deployment that is queued prunes the controller's deployment history.
`history_retention` (default 1000) bounds finished check-mode runs, which the
drift timer writes hourly. `deployment_retention` (default 100) bounds finished
real deployments: the newest that many are kept, together with the newest
successful full-fleet deployment of as many recently converged releases, so an
automatic `rollback` still has a target. Queued and running deployments, the
newest successful applied run, and any run a surviving rollback names are never
pruned. A rollback that names a pruned deployment by id is refused as not found.

Once nothing names a release, release pruning drops it, with its rendered
configuration bundle, beyond the newest `history_retention` releases. The
database therefore stops growing with every deploy.

`audit_retention` (`BLITZE_AUDIT_RETENTION`, default 100000, allowed 1000 to
10000000) bounds the audit log: the newest that many events are kept. It is a
disk bound, not a retention policy; the default holds years of activity, and a
trail kept longer belongs off the controller.

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
| `blitzecdn_certificates_preflight_blocked` | Sites awaiting a first certificate whose last preflight blocked |
| `blitzecdn_backup_last_success_timestamp_seconds` | Completion time of the last full backup, or zero when none is recorded |
| `blitzecdn_backup_last_success_size_bytes` | Size of that archive, or zero when none is recorded |
| `blitzecdn_convergence_last_success_timestamp_seconds{kind}` | Time of the last successful full-fleet run of that kind, or zero when none |
| `blitzecdn_convergence_last_failure_timestamp_seconds{kind}` | Time of the last failed full-fleet run of that kind, or zero when none |
| `blitzecdn_convergence_consecutive_failures{kind}` | Full-fleet runs of that kind failed since its last success |

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

### Deployment and drift-check alerts

The `blitzecdn_convergence_*` metrics carry a fixed `kind` label: `applied` for
real deployments and rollbacks, `drift_check` for check-mode runs such as the
scheduled drift check. Only full-fleet runs count; a canary (`--limit`) neither
raises nor resets the streak. An attempt refused before any deployment was
recorded, for example a release that would not compile, counts as a failure, so
a bad setting that blocks every deploy is still visible. The same figures are in
`GET /v3/status` and `blitzecdn overview` under `convergence`.

Example Prometheus rules; tune thresholds to your drift-check interval and how
often you deploy:

```yaml
groups:
  - name: blitzecdn-convergence
    rules:
      - alert: BlitzeCdnDriftCheckFailing
        expr: blitzecdn_convergence_consecutive_failures{kind="drift_check"} >= 3
        labels:
          severity: warning
        annotations:
          summary: The drift check has failed 3 or more times in a row.
      - alert: BlitzeCdnDeploymentsFailing
        expr: blitzecdn_convergence_consecutive_failures{kind="applied"} >= 2
        labels:
          severity: critical
        annotations:
          summary: Deployments keep failing; desired state is not reaching the edges.
      - alert: BlitzeCdnNoSuccessfulDeployment
        # Failing, and nothing has succeeded for 3 days.
        expr: |
          blitzecdn_convergence_consecutive_failures{kind="applied"} > 0
          and time() - blitzecdn_convergence_last_success_timestamp_seconds{kind="applied"} > 3 * 86400
        labels:
          severity: critical
        annotations:
          summary: No deployment has succeeded for 3 days while attempts fail.
```

## Installing, updating and removing a server

A server is installed from one file. Fetch `install.sh` for the release you
want and run its standalone command; it installs Docker Engine, pulls that
release's control-plane image, and runs everything else from the image. No
source tree, virtualenv or Git checkout is left on the host.

```sh
curl -fsSLO https://raw.githubusercontent.com/misaf/blitze-cdn-cp/vX.Y.Z/install.sh
gh attestation verify install.sh --repo misaf/blitze-cdn-cp --source-ref refs/tags/vX.Y.Z
sudo bash install.sh standalone --admin-cidr 203.0.113.8/32 --email ops@example.com
```

The second line checks the downloaded script before it runs as root; what the
script then checks itself is in [What the installer checks](#what-the-installer-checks).

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

### When an update fails

Edges keep serving through a failed update; only the API and worker are
affected. Before it rewrites anything, the update keeps the installed release's
Compose project, Compose environment, `blitzecdn.toml`, `blitzecdn` wrapper and
`blitzecdn-install` in `/etc/blitzecdn/previous-release`, and reads the
database's schema revision before it stops the services. If any step after that
fails, the host's files are put back to the installed release, and what happens
next depends on the schema:

- **Unchanged** (the update failed before migrating, or the migration rolled
  back): the previous release is started again on the database it left, and the
  update exits with the error that stopped it. Fix the cause and rerun
  `blitzecdn-install update`.
- **Migrated**: the previous release's code cannot run on the new schema, so the
  API and worker stay stopped. The message names the backup the update took;
  restore it with the previous release's wrapper and start the services:

  ```sh
  sudo blitzecdn backup restore /var/backups/blitzecdn/ARCHIVE.tar.gz --yes
  sudo docker compose --file /etc/blitzecdn/control-plane.compose.yml up --detach --wait
  ```

  Or fix the cause and rerun `blitzecdn-install update`. An update run with
  `--no-backup` has no archive to restore.

A `standalone` run converges the release its installer names, so it refuses a
host that runs a different one. To start again from a clean state, uninstall,
then run the standalone command of the release you want.

### What the installer checks

The installer runs the control-plane image as root, privileged and in the
host's PID namespace, so nothing it fetches is trusted on first sight:

- **install.sh.** Each release's image workflow attests the `install.sh` of its
  tag. `gh attestation verify` (run anywhere `gh` is signed in) shows the file
  is the one that release was built from; `--source-ref` is what ties it to the
  release you asked for, since `--repo` alone accepts any release's installer.
  The workflow's run summary also prints its SHA-256 for `sha256sum --check`.
- **The control-plane image.** The same workflow signs the image it publishes
  with cosign, keylessly, as itself for that tag. After pulling, the installer
  pins the digest the registry served and refuses to run it unless cosign
  shows `control-plane-image.yml` signed exactly that digest for the image's
  own release tag, with the signature in Sigstore's transparency log. cosign
  itself runs from an image pinned by digest in `install.sh`. This needs the
  host to reach Sigstore's public services as well as ghcr.io.
- **Docker's apt signing key.** Downloaded only when the keyring is missing,
  and installed only if it holds Docker's published key
  (`9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`) and no other. The
  `blitzecdn_docker` role checks the keyring on every converge.

An image loaded with `BLITZECDN_CONTROL_PLANE_ARCHIVE` or built with
`BLITZECDN_CONTROL_PLANE_BUILD=1` is the operator's own and is not
signature-checked; verify the image before saving it into an archive
(`cosign verify` with the identity above). A mirror that does not copy the
signatures (`cosign copy` does) needs `BLITZECDN_CONTROL_PLANE_VERIFY=0`;
name the image by the digest the release published, so the exact bytes are
still fixed:

```sh
sudo BLITZECDN_CONTROL_PLANE_VERIFY=0 \
  BLITZECDN_CONTROL_PLANE_IMAGE=mirror.example/blitzecdn-control-plane:X.Y.Z@sha256:DIGEST \
  bash install.sh standalone ...
```

## API network access

`allowed_ips` (`BLITZE_ALLOWED_IPS`, or `--allowed-ips` to `install.sh`) decides
which clients the API answers at all. It is not authentication: an allowed
client still presents `X-API-Key` on every control endpoint. Empty keeps the
listener on loopback, reachable only through an SSH tunnel. A non-empty list
binds the API publicly and answers 403 to every unlisted client, `/health` and
`/docs` included.

**Loopback is always allowed**, whatever the list says, so local health probes
and SSH tunnels keep working after a list change. The installed API runs with
`network_mode: host`, so this has two consequences:

- Any process on the controller host reaches the API as an allowed client.
  Treat local accounts on that host as inside the allowlist.
- A reverse proxy or TLS terminator on the same host makes every external
  request arrive from `127.0.0.1`, and the allowlist then filters nothing.
  Never put an unrestricted local proxy in front of the listener. If you need
  one, restrict client addresses in the proxy itself, or run it on another
  host and list that host's address.

The decision uses the transport peer only. `X-Forwarded-For`, `Forwarded` and
`X-Real-IP` never grant access, and uvicorn runs with proxy headers disabled.
When `allowed_ips` is set and a loopback request carries `X-Forwarded-For` or
`Forwarded`, the API logs one warning per process that a same-host proxy is
defeating the allowlist. The warning reports; it changes no decision.

## Edge SSH host keys

The controller connects to every edge with strict host key checking and
`BatchMode`, so it never trusts a key on first use and never prompts during a
deploy. An edge's key has to be trusted before its first deploy:

```bash
# On the edge's own console, read the fingerprint to compare against:
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub

# On the controller, pass it; only a matching key is trusted:
blitzecdn edge add edge-01 --host 192.0.2.10 --ssh-source 198.51.100.0/24 \
  --host-key-fingerprint SHA256:...
```

Without `--host-key-fingerprint`, `edge add` scans the host, prints the
fingerprints it offers and asks before trusting them. With no terminal to ask
on, it stops without registering the edge. `--skip-host-key` registers an edge
whose host is not up yet; trust its key before deploying.

`blitzecdn edge host-key NAME` scans a registered edge again and replaces every
key trusted for its address. Use it after the edge's host key is rotated or the
host is reinstalled, after `edge update --host` or `--port`, and for an edge
registered through the API, which does not scan. It takes the same
`--host-key-fingerprint` option and records who accepted which fingerprint in
the audit log.

The keys live in `/var/lib/blitzecdn/.ssh/known_hosts`, which a control-plane
update never rewrites. The installer keeps the loopback keys of a standalone
host in `known_hosts.loopback` beside it and rewrites only that file. A
checkout without the controller's SSH configuration uses your own SSH client
and `~/.ssh/known_hosts` instead, and `edge add` leaves host keys to it.

## The controller's SSH key

The controller deploys with one SSH identity, `/var/lib/blitzecdn/id_ed25519`
(`/opt/blitzecdn/.state/id_ed25519` inside the containers). An edge registered
without `--private-key-file` is reached with it. Ansible then becomes root on
every edge, so **whoever holds this key is root on the whole fleet**, and a
compromised controller is a compromised fleet. Protect it accordingly:

- **Where it lives.** The state directory is `0700` and the key `0600`, owned
  by the containers' numeric identity (65534). Root on the controller can read
  it, and so can anything that can read the state directory: a mounted volume,
  a container escape, a disk image.
- **Backups.** Every full backup holds the private key, so an archive is as
  sensitive as the key itself. Keep copies encrypted and access-controlled, as
  [Backups](#backups) says.
- **On a standalone host.** The host is its own edge: the role authorizes the
  key for the deployment account (`deploy`, with passwordless sudo), and the
  controller reaches it over loopback. That entry carries
  `from="127.0.0.1,::1",no-agent-forwarding,no-X11-forwarding,no-port-forwarding`,
  so a copy of the key does not log in from anywhere else. A converge adds the
  options to an entry that lacks them.
- **On other edges.** You put the public key in the edge account's
  `authorized_keys` yourself. Restrict it the same way to the controller's
  address, so the key is useless off the controller:

  ```text
  from="198.51.100.7",no-agent-forwarding,no-X11-forwarding,no-port-forwarding ssh-ed25519 AAAA... blitzecdn-local-controller
  ```

  The edge firewall already limits SSH to the edge's `--ssh-source` networks;
  `from=` narrows this one key further, to the one address that uses it.

Narrowing sudo to particular commands is not an option: Ansible's `become`
runs arbitrary module code as root.

### Rotating the key

There is no rotation command. To replace the key, for example after a backup
archive leaked:

1. Move the old pair aside on the controller:
   `sudo mv /var/lib/blitzecdn/id_ed25519{,.old}` and the same for `.pub`.
2. Run `sudo blitzecdn-install standalone ...` again with the installed
   options. The role creates a new pair and authorizes it for the loopback
   deployment account.
3. On every edge, add the new public key (with its `from=` options) to the
   deployment account's `authorized_keys`, then run `blitzecdn deploy` to
   check that each edge accepts it.
4. Remove the old public key from every edge, and from the standalone host's
   `~deploy/.ssh/authorized_keys`, which the role only adds to. Then delete
   the `.old` files.

Backups taken before the rotation still hold the old private key. Once the old
key is removed everywhere it no longer opens anything, but treat those archives
as sensitive until they age out.

## Host commands and Compose

The installed `blitzecdn` command runs disposable CLI containers through Docker
Compose. The host requires Compose 2.30+ and `flock` (util-linux); the Docker
installation supplies Compose and the control-plane role installs util-linux.
No application Python environment or source checkout is needed on the host.

Edit `/etc/blitzecdn/blitzecdn.env` for secrets. The wrapper and installer use
the application's parser to generate a root-only `compose.env` beside it.
Dollar signs are literal, shell quotes are removed once, and the first
assignment of a name wins. Do not edit `compose.env`; Compose reads it in raw
mode. Running a CLI command refreshes that generated file; persistent services
still need recreation to pick up changed credentials.

Backup commands support global options such as `blitzecdn --verbose backup
create`. Restores lock out competing restores, stop the previously running
API/worker services, publish configuration, then recreate that same service
set and wait up to 180 seconds for health. If recovery fails, the checkpoint
is retained and services stay stopped; retry `blitzecdn backup recover`.
That explicit recovery also starts existing stopped API/worker containers.

## GeoIP database

Country rules and the `BZ-IPCountry` header read a GeoLite2-Country database.
The controller downloads it from MaxMind once for the whole fleet, so the
account's daily download limit is spent per controller rather than per edge,
and no edge holds the MaxMind license key or contacts MaxMind.

1. Set `BLITZE_MAXMIND_ACCOUNT_ID` and `BLITZE_MAXMIND_LICENSE_KEY` in the
   controller's `.env`.
2. Turn the capability on: `blitzecdn config set blitzecdn_geoip_enabled true`.
3. Fetch the first database with `blitzecdn geoip update`, then `blitzecdn deploy`.

After that the `geoip-refresh` job downloads again every
`BLITZE_GEOIP_REFRESH_INTERVAL_SECONDS` (a day by default; `0` disables it).
A changed database is stored under the controller's state directory as
`geoip-databases/GeoLite2-Country-<sha256>.mmdb` and queues a fleet deployment. Each
release pins the file by digest; the edge role checks the digest on
the controller, copies the file, and checks it again on the edge. Files a
retained release still pins are kept, and full backups include them, so a
rollback ships the database it was built with even on a restored controller.

Without credentials, place a database at
`/var/lib/blitzecdn/edge-data/geoip/GeoLite2-Country.mmdb` on each edge by
hand; the deploy refuses an enabled edge that has none.

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
workflow's existing detail endpoint (`blitzecdn workflow list` / `show` on the
CLI) to inspect its retained journal.

A `needs_review` workflow is never pruned. Once you have checked the external
side (for example, whether a certificate was issued), acknowledge it with
`POST /v3/workflows/{workflow_id}/acknowledge` or
`blitzecdn workflow acknowledge WORKFLOW_ID`. It moves to `reviewed`, keeps its
error and checkpoints, records a `workflow.reviewed` audit event, and is then
pruned like any finished workflow.

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

## Origin addresses

Edges run Nginx in the host's own network namespace, so an origin is reached
from the edge itself. A proxied record whose address would point an edge at
itself is never served, whatever the zone says: loopback (`127.0.0.0/8`,
`::1`), link-local (`169.254.0.0/16`, `fe80::/10`, which holds the cloud
metadata service), the known metadata addresses outside it (`fd00:ec2::254`,
`100.100.100.200`), unspecified (`0.0.0.0/8`, `::`), multicast and reserved
addresses. IPv4-mapped IPv6 addresses are judged by the IPv4 address they carry.

Private addresses (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
`100.64.0.0/10`, `fc00::/7`) are served only when the zone or a rule sets
`allow_private_origin: true`, for an origin that really is on the provider's
internal network. Without it the hostname is reported as unservable, with the
reason, and the rest of the zone keeps serving.

Proxied records hold addresses, never names, so an edge never resolves an
origin and DNS rebinding cannot move one onto these ranges after the check.

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
agent; see [what access logs keep](#what-access-logs-keep) for how much of each
is recorded. String fields use Nginx JSON escaping; unavailable upstream values may be
`"-"`, and `cache_status` (`$upstream_cache_status`) is empty when the cache
was not consulted. Error logs retain Nginx's native
text format.

The visitor bandwidth counter sums `bytes_sent`: bytes Nginx reports
sending to the client, including response headers. `body_bytes_sent` excludes
response headers. These are application traffic measurements, not interface
counters or a provider's billable transfer total. Request counts count log
records, including redirects and errors. A long-running response is recorded
when its request finishes, so its traffic is attributed to that completion
time. See [Nginx's variable definitions](https://nginx.org/en/docs/http/ngx_http_core_module.html#variables).

The [traffic capability](traffic-accounting.md) collects these records and
persists hourly totals, status classes and each hour's busiest paths, which
[site analytics](traffic-accounting.md#site-analytics) reads.
Disabling access logging means there are no records to collect. Identity probes
suppressed by `access_log off` also remain excluded.
Cache statistics are counted from these records by traffic collection; see
[cache statistics](#cache-statistics). These files are the only request
log: there is no aggregate access file, and requests do not reach Docker
stdout, so `docker logs` shows startup output and errors only. Hostname errors
at `warn` and above go to their own file and Docker stderr; startup errors or
errors before Nginx has selected a hostname appear in the global `error.log`.
`blitzecdn_nginx_access_log_enabled=false` disables access logging for managed
hostnames, while error logging remains enabled. Release identity probes continue
to suppress access logging.

### What access logs keep

Access records hold personal data and, in URLs, sometimes credentials, so by
default they keep less than Nginx would. Three fleet settings control it, each
changed with `blitzecdn config set` and applied by the next release:

| Setting | Default | Effect |
| --- | --- | --- |
| `blitzecdn_nginx_log_query_strings` | `false` | `false` logs `request` as method, path and protocol, and `referer` up to its `?` or `#`. Query strings carry password-reset tokens, signed-URL signatures and API keys. The path is the one the visitor sent, before rewrites. `true` logs both in full. |
| `blitzecdn_nginx_log_client_ip` | `truncated` | `truncated` logs an IPv4 address as its /24 (`192.0.2.0`) and an IPv6 address as at most its /48 (`2001:db8:85a3::`). A `::` within the first three hextets keeps only what precedes it, which may be shorter than 48 bits but never longer. `full` logs the address. |
| `blitzecdn_nginx_log_retention_days` | `14` | How long rotated hostname logs and `error.log` are kept, in days, and the most archives kept per file. At least 1. |

The user agent, hostname, timestamps, byte counters, status and cache outcome
are always recorded. Traffic accounting and cache statistics read none of the
redacted fields, so these settings change no count. Site analytics' top paths
read the request path and always drop its query string at the edge, whichever
way `blitzecdn_nginx_log_query_strings` is set. Changing them affects new
records only; existing files and archives keep what they were written with
until rotation removes them. Error logs are Nginx's own text and may include a
client address and request line regardless of these settings; they share the
retention setting.

### Rotation

The Nginx role installs `logrotate`, enables its host timer, and manages
`/etc/logrotate.d/blitzecdn-nginx`. Hostname logs and the global `error.log`
rotate daily, or when larger than 100 MiB at a scheduled check. Archives older
than `blitzecdn_nginx_log_retention_days` are deleted, and no more than that
many are kept per file, so a file that rotates early for size is kept for less
time rather than more. Older archives are compressed; compression of the newest
archive is delayed. Rotation renames the file and asks the running container's
Nginx master to reopen logs, avoiding `copytruncate` and an Nginx restart. The
size threshold is checked when logrotate runs, so it is not a hard disk quota.
Docker's own stream is bounded separately by the json-file driver (3 × 32 MiB).

The hostname log directory is root-owned with group `adm` and mode `0751`, so
Nginx workers can traverse it when reopening files. Log files use mode `0640`,
owned by the runtime worker UID with group `adm`; other users cannot read them.
Removing a hostname leaves its logs intact. Edge teardown also retains logs by
default and removes the rotation policy; explicitly requesting
`blitzecdn_edge_teardown_remove_logs=true` removes hostname logs, the global
`error.log` and their archives as well. When overriding the runtime log root,
set `blitzecdn_edge_teardown_domain_logs_path` and
`blitzecdn_edge_teardown_error_log_path` to the same root with `/domains` and
`/error.log` appended before teardown. Logs remain local to each edge; this step adds no log
retrieval API or cross-edge statistics.

## Cache levels

A cached site has a cache level, and it decides what the edge stores when the
origin says nothing about caching:

- `standard` (the default) stores a response only when it is a static file type
  (by URL extension, such as `.css`, `.js`, images, fonts, media and archives, or
  by a `Content-Type` such as `image/*`, `font/*` or `text/css`), or when the
  origin gave it a lifetime with `Cache-Control`, `Expires` or
  `X-Accel-Expires`. A request carrying a cookie skips the cache, neither served
  from it nor stored in it, unless its URL is a static file. An HTML page is
  therefore never cached unless the origin asks for it.
- `everything` stores every 200, 301, 302 and 404 for `cache_valid_success` and
  `cache_valid_not_found`, whatever its type and whoever asked. Choose it only
  for an origin that marks per-visitor responses `Cache-Control: private`: one
  that renders a logged-in page without it would have that page served to the
  next visitor.

`cache_bypass_cookies` names the cookies that skip the cache; `*` matches any
run of name characters, as in `wordpress_logged_in_*`. Under `standard` it
narrows "any cookie" to those names, so analytics cookies no longer turn every
page into a miss; under `everything` it is the only cookie rule there is.

```bash
blitzecdn domain cache example.com --level standard --bypass-cookie sessionid
blitzecdn domain cache example.com --clear-bypass-cookies
```

Whatever the level, a request with `Authorization` or a WebSocket upgrade is
never cached, and nginx still refuses to store a response with `Set-Cookie` or
`Cache-Control: private`, `no-cache` or `no-store`. Bypassed requests count as
`BYPASS` in [cache statistics](#cache-statistics).

### Edge TTL overrides

`cache_ttl_overrides` sets the edge TTL by URL extension (`.css`), content type
(`text/html`) or type family (`image/*`), in place of `cache_valid_success`:

```bash
blitzecdn domain cache example.com --ttl 'image/*=7d' --ttl text/html=0 --ttl .css=1h
blitzecdn domain cache example.com --clear-ttls
```

An override applies to 200, 301, 302 and 304 responses whose origin sent no
`Cache-Control`, `Expires` or `X-Accel-Expires`; when the origin did, the origin
decides. `0` stores nothing of that type. An extension wins over the
`Content-Type` the origin labels the response with, and an exact type over a
family. A TTL is whole seconds, minutes, hours, days or weeks. Under the
`standard` level an override also lets a type be stored that otherwise would
not be, such as `text/html=5m`; the cookie bypass still applies.

`cache_ignore_origin_headers` lists extensions, types or families whose origin
caching headers the edge disregards, for an origin that marks its assets
`no-cache` by mistake:

```bash
blitzecdn domain cache example.com --ignore-origin-headers 'image/*' --ignore-origin-headers .js
blitzecdn domain cache example.com --respect-origin-headers
```

A listed type is stored for its override if it has one, else for
`cache_valid_success` (404: `cache_valid_not_found`), and any other status of it
is not stored, even with `Cache-Control: private` or `no-store`. Only list types
that are the same for every visitor. A `Set-Cookie` response is still never
stored, and visitors receive the origin's headers unchanged.

A site with an entry naming a type (rather than an extension) does not
revalidate expired entries with a conditional request: a `304` need not carry
`Content-Type`, so the edge could not tell which rule it renews. Such entries
are refetched in full instead.

How it works: nginx's `proxy_cache_valid` and `proxy_ignore_headers` cannot
vary per response, but nginx takes a cached response's lifetime from
`X-Accel-Expires` before `Cache-Control` or `Expires`. A site with either setting
reaches its origin through a loopback hop on `127.0.0.1:8091`
(`blitzecdn_nginx_origin_hop_port`) that rewrites `X-Accel-Expires` before the
cache reads it. Sites without either setting reach the origin directly, as
before.

## Error pages and maintenance mode

`error_pages` replaces what visitors see when a site fails, by status: `500`,
`502`, `503` and `504` from the origin, or from the edge when the origin cannot
be reached or times out, and `403` and `429` from the edge's own firewall and
rate limit. The status is kept.

```bash
blitzecdn domain error-pages example.com --page 502=./502.html --page 504=./504.html
blitzecdn domain error-pages example.com --clear
```

Only the statuses a site has a page for are replaced. An origin's own `403`,
`404` or `429` reaches visitors as the origin sent it; a `5xx` page replaces
the origin's `5xx` body too, except with Under Attack Mode, where the origin's
`5xx` pass through (replacing them would also replace an origin's `401`) and only
the edge's own `502` and `504` get the page. Pages ship in every configuration release, under
`pages/<site>/` in the release's Nginx tree (`blitzecdn_nginx_pages_dir`), and
are served from the edge's disk with `Cache-Control: no-store`. Each is at most
64 KiB; keep them self-contained, with inline styles and images, since the
origin may be what is down.

Maintenance mode answers every request with the maintenance page, `503` and
`Retry-After`, without contacting the cache or the origin:

```bash
blitzecdn domain maintenance example.com --page ./maintenance.html --retry-after 600
blitzecdn domain maintenance example.com --on
blitzecdn domain maintenance example.com --off
```

Networks in `firewall.allow_sources` are exempt and reach the origin as usual,
so the owner can check the site before turning maintenance off. Without a
`--page`, maintenance serves the site's `503` error page, else a plain built-in
one. An HTTP listener that redirects to HTTPS keeps redirecting, and ACME
challenges are still answered.

Both change only what visitors see. They do not check the origin's health or
fail over, and a deploy applies them like any other setting.

## Cache statistics

Cache effectiveness is read from stored hourly rows, never by sampling edges.
Traffic collection counts each access record's cache outcome exactly once, in
the same pass and transaction as its traffic, so the two never disagree about
what was counted. See [traffic accounting](traffic-accounting.md#cache-outcomes).

```sh
blitzecdn cache stats \
  --start 2026-10-27T14:00:00Z --end 2026-10-27T15:00:00Z \
  --hostname example.com --json
```

`GET /v3/cache/stats` takes the same `start`, `end`, `hostname`, `edge`, `limit`
and `offset` as `/v3/traffic/usage` and returns one row per edge, hostname and
UTC hour with:

- `requests`, `bytes_sent` and `outcomes` (requests and bytes per outcome);
- `cacheable_requests`: every outcome except NONE;
- `hits` and `hit_ratio`: HIT, STALE, UPDATING and REVALIDATED over cacheable
  requests;
- `cached_bytes_sent` and `byte_hit_ratio`: bytes served from cache, the
  origin offload a request-count ratio does not show.

Ratios are `null` when nothing consulted the cache: an idle site is not a broken
one. Redirects, errors Nginx served itself and sites with caching disabled are
NONE rather than misses. Rows lag while collection is pending; check
`blitzecdn traffic status EDGE`. To aggregate a range or a site across edges,
sum the counters, not the ratios, or read
[site analytics](traffic-accounting.md#site-analytics), which does.

Nginx's `stub_status` connection counters are live state, not log-derived:
`POST /v3/cache/connections` queues a read (the operation's result is a
`ConnectionsReport`), and `blitzecdn cache connections` runs one directly.
