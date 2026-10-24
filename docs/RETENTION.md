# Retention and recovery copies

Keep the recovery archive, the releases it describes and their exact keys
together. A retained deployment lets an intact controller roll back; it cannot
rebuild a lost controller. A full backup can rebuild the controller, but it does
not restore an edge's running Nginx process.

## Rule for this milestone

Keep all full backup archives and all real deployment history. Keep an off-host
copy of each archive used in a successful recovery rehearsal. Do not introduce
automatic archive or edge-file deletion until real edge recovery has been
verified. Controller certificate collection remains an explicit operation.

This deliberately favors recovery over a fixed disk limit while the system is
unused. It is not a promise of a particular number of days of recovery. Before
production, choose both the acceptable loss of recent changes (which determines
backup frequency) and how far back recovery must reach (which determines archive
retention). Counts of deployments or logs cannot establish either time window.

## What is kept automatically

`history_retention` defaults to **1,000**. It is a separate count for each
category below, not one shared budget. Collection happens during normal work,
not immediately when a setting is lowered.

| Data | Retention rule | When cleanup happens |
| --- | --- | --- |
| Real deployments and their edge progress | All are kept, including failures and limited deployments. Queued/running work is never history to discard. | No automatic deletion. |
| Check-mode deployments (including drift checks) | Newest 1,000 finished checks, ordered by creation time; queued/running checks and their edge progress survive regardless of age. | Admission of another deployment. |
| Releases | Newest 1,000 plus every release named by any retained deployment. Canonical input rows are conservatively retained even after their release is removed. | Preparing a release. |
| Durable jobs | Newest 1,000 succeeded/failed jobs, ordered by creation time. Pending/running jobs survive, including retries waiting for their next attempt. | After a worker executes a claim. A crash may defer cleanup until later work. |
| Workflow journals | Newest 1,000 terminal entries, ordered by creation time. Pending/running entries survive. `needs_review` is terminal and can age out; inspect and preserve incident evidence promptly. | Normal exit from a coordinated workflow, including an outcome reported as failed. An exception or startup reconciliation alone does not trigger pruning. |
| Raw Ansible logs | Newest 500 `.log` files by modification time (`run_log_retention`). A retained deployment may outlive its raw log; its structured result remains in the database. | Completion or handled launch failure of an Ansible invocation. |
| Audit events | Newest 100,000 events by increasing event ID. A backup contains the trail still retained when it was made. | Recording another event. |
| Backup archives | All full and selected-component archives. They are not rotated or expired by the application. | Operator-managed; no application cleanup. |
| Edge configuration trees, TLS files and activation journals | Conservatively retained. Controller collection does not remove them. | No age-based cleanup in this milestone. |

Counts are not byte limits. Real deployments, canonical inputs, archives and
referenced certificate versions can continue growing. SQLite row deletion also
does not promise immediate reduction of the database file's size. Monitor both
disk usage and free space rather than treating these settings as a disk quota.

Configure the two operator-facing counts in `blitzecdn.toml`:

```toml
[blitzecdn]
history_retention = 1000
run_log_retention = 500
```

The corresponding environment variables are `BLITZE_HISTORY_RETENTION` and
`BLITZE_RUN_LOG_RETENTION`. Accepted ranges are 50–100,000 and 10–100,000,
respectively. The audit count is currently an application `Settings` default;
there is no TOML/environment option for it. Changing these counts does not prune
backups or real deployment history.

## Which certificate files may be removed

`blitzecdn cert collect --json` deletes only superseded, owned,
content-addressed PEM files on the controller that no root still names. Roots
include all current zone/rule policy (even inactive rules), **every retained
release**, and the latest valid metadata pointer for each certificate. It
validates those roots before deleting; damaged roots stop collection. Unknown
files and symlinks are skipped.

Deleting a site does not remove its latest metadata or key pair. Keeping a real
deployment also keeps its release and the exact managed key/certificate versions
needed for rollback. Never remove those files manually just because a certificate
has been renewed. Expired certificates can be preserved correctly yet still be
unsuitable for serving HTTPS; retention does not extend their validity.

There is no scheduled certificate collection, metadata expiration or edge
collection. See [decision 0015](decisions/0015-canonical-certificate-versions-and-collection.md)
for the synchronization and interrupted-deletion guarantees.

## What must travel with a recovery copy

Use `blitzecdn backup create` without `--only` for controller disaster recovery.
It includes each component that exists: database, TLS material, ACME state and
portable configuration, including stored API credentials and controller SSH
identity. A component absent on the controller is absent from the archive. Check
the manifest with `blitzecdn backup inspect /path/to/backup.tar.gz`.

Components are exported sequentially, not as one transaction across the database
and filesystem. Take a recovery copy while authoring, issuance, deployment and
certificate collection are quiescent so they cannot change its references during
creation. SQLite's consistent database copy alone does not guarantee that the
other components represent the same instant.

A database-only archive is useful before a database change, but does not carry
the private keys or configuration needed for rebuilding the host. Installer
`update` takes a database-only backup; `upgrade` takes a full one unless backup
is skipped. Neither replaces a regular full-backup policy.

The full archive excludes raw logs, runner scratch files, remote edge state,
machine-specific paths and external resources such as externally managed keys
or container images. Preserve any external material and the compatible
application/image versions needed by the installation separately. Export incident
logs before they age out if they matter to an investigation.

Backups normally go under the controller state directory's `backups/`; managed
installations configure `/var/backups/blitzecdn`. `BLITZE_BACKUP_DIR` or the
backup capability's `backup_dir` TOML key can select a host-specific destination.
Archives have mode 0600 and the default destination directory has mode 0700;
off-host copies need equivalent access protection because they carry secrets.

Before deleting an archive manually, have a newer full copy off-host and restore
it into a separate empty controller directory. Verify the intended release,
configuration and keys using the [recovery practice](LOCAL_RECOVERY_PRACTICE.md).
`backup inspect` validates archive structure and schema identity; it is not a
substitute for a restore rehearsal or a real edge check. Never delete the last
successfully rehearsed recovery copy to make room for an unverified replacement.

## Estimate space before setting a time window

Measure the compressed full archive and the live database/TLS/ACME directories
after representative changes. Budget for retained archives plus a new archive,
uncompressed staging and restore copies, live state, logs and free space for
SQLite/normal operation. No fixed compression ratio or total byte cap is enforced.

For example, daily archives kept for 14 days mean at least 14 archive sizes in
the archive store, with additional space for creation/restoration. This is sizing
arithmetic, not an enabled schedule or an adopted 14-day policy. Re-measure after
the state grows. If space is tight during this milestone, move verified archives
off-host before considering any deletion of recovery data.

## Evidence

The repository tests exercise finished-check cleanup while preserving real
deployments, unfinished queued/running checks and their target rows; actual
worker ticks bound both successful and failed job history without removing live
work. Release, workflow, audit, log, backup and certificate tests cover their
respective rules. Remote execution results for this step are recorded in
[REIMPLEMENTATION.md](REIMPLEMENTATION.md). No production data is cleaned by
these tests; they use temporary controllers. Real edge cleanup/recovery remains
deferred.
