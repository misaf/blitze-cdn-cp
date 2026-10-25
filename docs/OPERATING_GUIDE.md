# Operating and recovering the controller

Use this with an initialized controller and its configured CLI. Replace uppercase
IDs and example paths. `blitzecdn setup` prepares a new controller; ordinary
commands require an existing database. Do not run setup before restoring a full
archive into an empty directory.

Controller state, backup/restore, worker interruption and rollback have been
exercised with simulated edges. Real Ubuntu installation, Nginx activation,
Compose recovery and ACME remain unverified. The separately authorized
[real-edge rehearsal](REAL_EDGE_REHEARSAL.md) is in preparation. The edge-changing
commands below describe the controller-tested flow, not a completed real-edge
rehearsal.

## Check what happened

```bash
blitzecdn doctor
blitzecdn status
blitzecdn job list --json
blitzecdn audit
```

`doctor` reports configuration and readiness findings; it is not proof that a
site is serving. `status` lists recent deployments. Record the deployment ID,
release ID and error before changing anything. Inspect one deployment's result
and each edge's progress with:

```bash
blitzecdn status DEPLOYMENT_ID
```

For an applied deployment, `succeeded` requires observing the expected served
release. A successful Ansible reload alone is insufficient. Failed or unreachable
edges need investigation; the fleet may be serving different releases. Keep the
referenced run log before log rotation removes it. BlitzeCDN records desired DNS
but does not publish it;
the `dns_publication` finding requires action at your authoritative DNS provider.

## Back up before a change

Pause authoring, issuance, deployment and collection while creating the recovery
copy: components are exported sequentially. Keep the archive outside the
controller directory and copy it off-host with access restricted to operators.

```bash
blitzecdn backup create --output /absolute/path/controller.tar.gz
blitzecdn backup inspect /absolute/path/controller.tar.gz
```

Use a new filename each time; existing archives are never overwritten. A full
archive includes each component that exists. Confirm the manifest lists the
database and, when present, TLS, ACME and configuration. A database-only archive
cannot replace the full recovery copy. Archives contain credentials/private keys.

## Apply or undo a change

After editing the desired state, the normal controller flow is:

```bash
blitzecdn validate
blitzecdn plan
blitzecdn deploy
blitzecdn status DEPLOYMENT_ID
```

Stop at a validation or preview failure. Both validation and planning can invoke
Ansible; planning contacts the configured edges in check mode. `deploy` repeats
validation/preview and asks for confirmation before applying. Keep its result and
verify the per-edge outcome. CLI deploy/rollback wait for a result; background
submissions appear in the job queue. BlitzeCDN does not promise automatic fleet
rollback after every failure.

To return to a known successful applied deployment (`check_mode: false`):

```bash
blitzecdn status GOOD_DEPLOYMENT_ID
blitzecdn rollback GOOD_DEPLOYMENT_ID --check
blitzecdn rollback GOOD_DEPLOYMENT_ID
blitzecdn status
```

The rollback preview contacts edges too. Rollback targets the current fleet,
even when the chosen deployment was limited. Prefer a known fleet-wide success;
a limited deployment proves only that its selected edges ran the release.
Successful rollback also replaces canonical desired state with that retained
snapshot, so preserve newer edits in a backup first. Inspect the new rollback
deployment by its ID. Missing or corrupt selected certificate material must be
repaired; the controller will not silently substitute the latest key pair.

## A worker stopped during work

```bash
blitzecdn job list --status running --json
blitzecdn job show JOB_ID --json
blitzecdn status DEPLOYMENT_ID
```

For deployment jobs, find `DEPLOYMENT_ID` in `payload.deployment_id`.
`leased_by` records the worker host/process; check whether it is still alive.
If it stopped, a healthy worker can reclaim the same job only after
`leased_until`. Deployment leases default to 900 seconds; scheduled jobs may
differ. Restarting does not clear the lease.

Inspect the same job again after its deadline. Attempts may increase as ownership
changes. Confirm the deployment's result and edge progress, not just lease expiry.
If it stays stuck or becomes failed, inspect worker health/logs, `last_error`,
attempt limits and lock contention. Do not edit queue rows, delete lock files or
submit duplicates to bypass waiting. Resume/retry is bounded; a terminal failure
needs investigation rather than a claim that recovery succeeded.

A queued deployment waiting for another deployment's lock remains a pending
job. Inspect its `available_at` and `last_error`: the worker tries again after
five seconds without spending an execution attempt. It resumes the same job
when the lock becomes available; no API restart or duplicate submission is
needed. Failures after execution starts, and expired claims left by dead
workers, still use the bounded retry budget.

## Rebuild from an archive

The following is the demonstrated **source-checkout CLI** restore path. Use a
compatible application version in a clean shell without old `BLITZE_*` path
overrides. Keep the archive outside both controller directories and stop any
API/worker using the destination. Create a separate empty directory:

```bash
mkdir /absolute/path/rebuilt-controller
export BLITZE_PROJECT_DIR=/absolute/path/rebuilt-controller
blitzecdn backup inspect /absolute/path/controller.tar.gz
blitzecdn backup restore /absolute/path/controller.tar.gz
blitzecdn edge list --json
blitzecdn domain list --json
blitzecdn status
```

Restore asks before replacing the components declared in the archive. Confirm
the expected inventory, desired state, deployment/release history, credentials
and exact TLS files; private keys should have mode 0600. Keep the old recovery
copy until the replacement has passed a restore rehearsal.

Restore keeps the previous selected components in a private `restore-recovery/`
directory under the controller state directory. A component failure restores
those copies, including credentials, symlinks, permissions and previously absent
files. On an installed host they remain available until host configuration has
also been published. Allow disk space for these copies as well as archive staging.

If recovery itself fails, the installed wrapper leaves services stopped and
reports the retained checkpoint. Do not delete it or start another restore.
After resolving the reported filesystem problem, run:

```bash
blitzecdn backup recover
```

Use the host wrapper on an installed controller. For a source checkout, stop
its API and worker before this command and restart them only after it succeeds.
Recovery returns to the state before the attempted restore; it does not retry
installing the archive. These checkpoints support interrupted process recovery,
not a guarantee against disk loss or power failure.

Restoring the controller does not change the running edges. Review validation
and plan before any subsequent deploy; actual edge recovery still needs the
deferred rehearsal. On a managed installation use the **host** `blitzecdn backup
restore` wrapper, which owns stopping/recreating services. That Compose lifecycle
has not been demonstrated here; do not run a raw container restore or set
`COMPOSE_RESTORE_OFFLINE` manually. Restored API access policy also needs host
firewall reconciliation; see the restore command's output and
[installed lifecycle](../README.md#installed-command-and-restore-lifecycle).

## Keep the way back

Keep full archives and real deployment history for this milestone. Finished jobs,
checks, workflows, audit events and logs have separate count limits. Exact keys
needed by retained releases must survive; renewed or expired does not mean safe
to delete. Follow [retention rules](RETENTION.md) before considering collection.

The [acceptance checklist](RECOVERY_ACCEPTANCE.md) and
[recovery practice](LOCAL_RECOVERY_PRACTICE.md) identify the tests, observed
results and remaining limits. No production recovery-time guarantee is implied.
