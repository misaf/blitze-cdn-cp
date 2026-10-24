# Operating and recovery acceptance checklist

Step 1: define what success means and check the evidence already available.
Reviewed against application commit `ad2c1e1` on 2026-10-24. Step 1 changed
only documentation. Subsequent fresh-process evidence is recorded in
[LOCAL_RECOVERY_PRACTICE.md](LOCAL_RECOVERY_PRACTICE.md); its explicit limits
apply alongside the original test inventory below.

## What we want to be able to trust

A controller should preserve its state, tell the operator what happened, and
provide a clear way to recover interrupted work or return to a known release.
A passing command alone is not enough: the resulting state must be checked.

The table separates local automated evidence from situations still to be
practiced. “Locally checked” does not mean verified on an Ubuntu edge.

| Situation | Acceptance criterion | Evidence and current limit |
| --- | --- | --- |
| Prepare a fresh controller | Setup creates a usable database and credentials. Repeating setup preserves existing credentials and registered edges. Ordinary commands never silently initialize a missing database. | Locally checked using the installed wheel in a fresh environment. Host installation, SSH and Compose startup remain deferred. See A. |
| Deploy a site | The saved deployment names the compiled release, and success requires the endpoint to report that exact release. A reachable endpoint serving an old release must fail verification. | Locally checked using production persistence, compilation and HTTP verification. Ansible activation is simulated; this does not prove Nginx was configured correctly. See B. |
| Resume interrupted deployment work | Committed work remains discoverable. Once the old worker's lease expires, another worker can resume it. Completed edges are checked again before their saved progress is trusted. | Locally checked by constructing interrupted state and advancing a test clock. Those tests do not kill and restart an OS worker process. Subsequent local practice did so at one pre-activation checkpoint; see C and the practice record. |
| Prevent a stale worker from finishing | A worker that has lost ownership cannot mark another worker's job complete or renew its old claim. Retries are bounded and exhausted work is reported as failed. | Locally checked against real SQLite stores with controlled clocks and handlers. The subsequent practice covers one SIGKILL/reclaim boundary, not all crash timing. See C. |
| Roll back a change | An operator can select a retained deployment and return both desired configuration and the served release to it. Managed certificates use that release's exact material, rather than the newest files. | Locally checked through a simulated edge and certificate operations. This is operator-requested rollback, not a promise that every failure automatically restores the fleet. See D. |
| Rebuild a lost controller | A full backup can be restored into a separate empty location. Restored desired state regenerates the same configuration; credentials, keys and ACME files return with it. | Locally checked using real archive and filesystem operations. The fixture includes synthetic key/account contents; it does not prove real CA account access, service restart or edge connectivity after restore. See E. |
| Refuse an unsafe restore | Invalid archives and unsupported or mismatched schema identities are rejected before live data is modified. Selected-component restore leaves other components alone. | Locally checked with malformed archives and temporary files. Service stop/start failure tests use a recording double, not Docker Compose. See E. |
| Report certificate trouble | Failed preflight or renewal is reported as failure. An interrupted operation with an uncertain outcome is surfaced for review. Material needed by retained releases survives collection. | Local tests cover these rules. Real ACME issuance, renewal and continued HTTPS service after recovery remain deferred. See F. |

## Evidence to inspect or repeat

These are existing tests, not new demonstration scripts. Links point to the
owning files; test names identify the relevant cases.

**A — Fresh controller.**
[Installed-wheel lifecycle](../tests/architecture/test_application_installation.py):
`test_installed_wheel_requires_explicit_schema_preparation`.
[Schema lifecycle](../tests/platform/test_migrations.py) also checks that startup
refuses absent, unprepared and damaged schemas without upgrading them.

**B — Deployment and observation.**
[Control-plane journeys](../tests/journeys/test_complete_path.py):
`test_the_whole_path_from_an_empty_control_plane_to_a_verified_edge`,
`test_an_edge_that_stops_serving_fails_the_deployment_that_touched_it` and
`test_a_healthy_edge_that_did_not_activate_the_new_release_fails_verification`.
The HTTP listener runs in a thread; it is not an Nginx container or an edge OS.

**C — Interrupted work and ownership.**
[Deployment queue](../tests/capabilities/deployments/test_queue_workflow.py):
`test_a_deployment_its_worker_died_in_resumes_on_redelivery`,
`test_startup_republishes_an_interrupted_deployment_for_resumption`,
`test_a_deployment_and_its_job_commit_together_or_not_at_all` and
`test_worker_resumes_checkpoints_and_reobserves_completed_edges`.
[Job ownership](../tests/capabilities/jobs/test_job_queue.py):
`test_a_worker_whose_lease_expired_cannot_complete_its_job`,
`test_a_failing_job_retries_with_bounded_backoff` and
`test_a_job_that_keeps_killing_its_worker_fails_once_its_attempts_are_spent`.
Their names describe the modeled situation; they do not establish a real
process-kill demonstration.

**D — Rollback.**
[Rollback journey](../tests/journeys/test_complete_path.py):
`test_a_failed_update_is_recovered_by_rolling_back_the_desired_target`.
The test deploys an unwanted change successfully and then explicitly rolls back;
it does not inject a failed Nginx activation.
[Certificate material](../tests/capabilities/certificates/test_material_lifecycle.py):
`test_deployment_history_keeps_material_and_rollback_restores_its_version`.

**E — Restore.**
[Backup recovery](../tests/capabilities/backup/test_backup.py):
`test_a_full_backup_rebuilds_a_controller_from_nothing`,
`test_a_restored_database_is_consistent_and_migrated`,
`test_validation_happens_before_anything_is_modified`,
`test_services_come_back_even_when_a_restore_fails` and the archive validation
and selected-component round trips in that file. The lost-controller test
restores to an empty destination; it does not destroy and reprovision a host.

**F — Certificate failure and preservation.**
[Preflight and renewal](../tests/capabilities/certificates/test_preflight_workflow.py):
`test_a_blocked_preflight_refuses_before_reaching_the_ca` and
`test_a_blocked_renewal_is_reported_as_failed_not_silently_skipped`.
[Material preservation](../tests/capabilities/certificates/test_material_lifecycle.py)
also checks retained references, corrupt metadata and interrupted collection.
[Startup recovery](../tests/entrypoints/test_control_plane.py):
`test_startup_recovery_requeues_what_a_dead_process_left_behind` and
`test_startup_recovery_leaves_a_live_deployment_alone` distinguish uncertain
interrupted workflows from work still owned by a live deployment.
[Schedules](../tests/capabilities/certificates/test_scheduled_jobs.py) checks
persisted intervals; it does not wait for real CA renewal.

## Follow-up work and status

1. **Local process recovery completed in step 2.** The
   [practice record](LOCAL_RECOVERY_PRACTICE.md) demonstrates one SIGKILL/resume
   checkpoint, CLI restoration after controller-directory removal and explicit
   rollback from a rebuilt controller. Edge activation is still simulated.
2. **Operator instructions corrected in step 3.** The README authoring commands
   and job inspection help were exercised remotely; the
   [practice record](LOCAL_RECOVERY_PRACTICE.md) records the results and limits.
3. **Retention rules documented in step 4.** See
   [retention and recovery copies](RETENTION.md) for exact cleanup counts,
   preservation of releases/keys, full versus partial archives, and storage
   budgeting. The milestone retains all full archives and real deployment
   history; a production recovery window still needs an operator's decision.
4. **Short operating guide completed in step 5.** The
   [guide](OPERATING_GUIDE.md) brings together controller inspection, backups,
   interrupted work, rollback and restore, with the real-edge boundary stated
   explicitly. Its CLI and disposable restore checks ran remotely; see
   [the validation record](REIMPLEMENTATION.md#step-5-validation).

The user subsequently authorized the real-edge rehearsal. Its
[preparation record](REAL_EDGE_REHEARSAL.md) covers isolation, image preparation
and the remaining network limitations. Ubuntu installation, real Nginx
activation/rollback, Compose service recovery and ACME integration are not yet
verified. The repository has an
[Ubuntu installer integration harness](../tests/container-install.sh), but the
existence of that harness is not evidence that it was run for this checkpoint.
Local demonstrations cannot close those environment-specific gaps.

## Verification record

The commands and results for this documentation checkpoint are recorded below.
Only existing local tests are run; no real server is provisioned or changed.

Run on 2026-10-24 against `ad2c1e1`: **171 tests passed** across two selections.
These are focused runs without coverage measurement; the previous full-suite
checkpoint remains separate in [REIMPLEMENTATION.md](REIMPLEMENTATION.md).

Main selection: **142 passed**.

```bash
just test-one \
  tests/architecture/test_application_installation.py \
  tests/platform/test_migrations.py \
  tests/journeys/test_complete_path.py \
  tests/capabilities/backup/test_backup.py \
  tests/capabilities/deployments/test_queue_workflow.py \
  tests/capabilities/jobs/test_job_queue.py \
  tests/capabilities/certificates/test_scheduled_jobs.py
```

Supplemental selection: **29 passed**.

```bash
just test-one \
  tests/capabilities/certificates/test_material_lifecycle.py \
  tests/capabilities/certificates/test_preflight_workflow.py \
  tests/entrypoints/test_control_plane.py::test_startup_recovery_requeues_what_a_dead_process_left_behind \
  tests/entrypoints/test_control_plane.py::test_startup_recovery_leaves_a_live_deployment_alone
```

Step 2 subsequently completed the fresh-process local recovery practice, with
its results and limitations in [LOCAL_RECOVERY_PRACTICE.md](LOCAL_RECOVERY_PRACTICE.md).
Step 3 subsequently corrected the operator instructions. Step 4 documents
retention rules and verifies cleanup against the preserved recovery paths;
its validation record is in [REIMPLEMENTATION.md](REIMPLEMENTATION.md).
Step 5 subsequently completed the short operating guide. All five milestone
steps are complete; the real-edge rehearsal remains separate, incomplete work.
