# Reimplementation sequence

This application has no deployed compatibility obligations. Existing behavior is
evidence of requirements, not a requirement to preserve its interfaces or
packaging. Each completed step receives an independent commit and verification.

1. Immutable canonical release documents and verified persistence.
2. Explicit preparation inputs and a compiler that accepts values only.
3. Immutable certificate versions retained for release rollback.
4. Persisted deployment targets and explicit reconciliation outcomes.
5. Staged configuration activation and release identity verification.
6. Serving policy composition and shared application operations.
7. A single application distribution with focused runtime providers.
8. Installation lifecycle, end-to-end recovery, and final verification.

The implementation proceeds through working vertical slices. A step is complete
only when its production path uses the new behavior, its tests pass, and the
associated decisions describe the implementation that actually exists.

## Progress

- Baseline: clean `5.x` checkout at `1123bd7`; working branch
  `codex/reimplementation`.
- Previous source assessment: 135 focused architecture, release, rollout, and
  durable job tests passed. This is not a real-edge deployment verification.

- Step 1 complete: immutable canonical artifacts and compiled variables; input
  matching and stored digest verification. Formatting, strict types, shell and
  Ansible checks passed. Broad suite: 1,821 passed, 12 skipped; its sole failure
  was the intended SDK additions, whose reviewed baseline and focused retest pass.

- Step 2 complete: value-only compilation over a serializable preparation
  snapshot. Canonical state and edge declarations are captured together; plugin
  checks and resource lookup occur before compilation. Replay and mutation
  regressions pass; formatting, strict types, and 137 affected tests pass.

- Step 3 complete: certificate chains and keys use immutable content identities;
  replacement retains old versions, and deployments verify captured digests.
  Formatting and strict types pass; 124 certificate tests and two executable
  Ansible material-integrity tests pass. Historical material is retained
  conservatively; reference-aware garbage collection remains a later operation.

- Step 4 admission complete: queued deployments expose their persisted target
  rows immediately; later fleet additions cannot expand execution, and missing
  admitted edges fail before a run starts. Formatting, strict types, and 133
  deployment, release, and durable-job tests pass. Recovery and verification
  semantics continue in step 5.

- Step 5 verification complete: virtual hosts expose their full release digest;
  the controller refuses stale configuration and missing verification addresses.
  Execution envelopes carry identity outside the artifact hash. Formatting and
  strict types pass; 121 release, rollout, live HTTP/TLS, and rendered-template
  tests pass. The subsequent activation slice below completes configuration
  staging and reversion.

- Step 4 recovery complete: startup republishes interrupted RUNNING work;
  redelivery resumes durable phases under a new generation and re-observes
  completed edges before trusting them. Explicit graceful abandonment remains
  terminal. Formatting, strict types, and 111 recovery, deployment, entry-point,
  and durable-job tests pass.

- Recovery hardening: rollout takeover now atomically invalidates old target
  tokens, and stale planning cannot restore them. The executable material check
  also passes the Ansible production lint profile with role-scoped variables.

- Journey verification: the real HTTP probe now observes activation, rollback,
  and a simulated stale edge through the full control-plane path. Deployments
  persist their own verification error independently of a successful Ansible
  result. The initial schema and HTTP contract intentionally add that field;
  97 affected journey, deployment, API, and persistence tests pass.

- Foundation checkpoint: the complete workspace fast suite passed (1,836 passed,
  12 skipped); all eleven wheels and source distributions built successfully.
- Step 6 ownership: serving policy, visitor/origin contracts, and effective
  virtual hosts move into `capabilities/serving`; DNS retains authoring and host
  derivation. Consumers import the new owners directly, without compatibility
  facades. The subsequent nested-policy slice completes composition; shared
  application commands remain separate work.
  Ownership validation: strict types and formatting pass; 511 architecture,
  package-boundary, policy, DNS, release, and public-contract checks pass with
  13 environment-dependent skips.

- Core-only verification passes: 1,268 passed and 35 skipped with all ten optional
  distributions removed. The static Ansible example compares artifact content
  independently of the installation-specific release digest; publication tests
  assert the exact digest against the stored release. The workspace environment
  was restored after the run.

- Step 5 activation implemented: render and seal retained candidate trees,
  validate with a captured immutable image, atomically switch the active pointer,
  and retain a durable rollback journal for configuration and runtime records.
  External release verification commits the journal; activation or observation
  failures withdraw it. First-install failure and failed rollback have explicit
  outcomes. The Ansible surface intentionally adds the configuration-release
  role; all other frozen surfaces are unchanged. See decision 0012.

- Step 6 composition implemented: zones and virtual hosts contain a frozen
  `SitePolicy` made of named capability values. Requirements are collected from
  contained values, with duplicate setting ownership refused. Canonical policy
  JSON is nested; release input/compiler versions are 2 and retired inputs are
  rejected. Explicit conversions preserve API/CLI and edge document settings,
  patch/rule semantics and omission rules. All frozen surfaces are unchanged.
  See decision 0013.

- Shared application policy commands implemented: immutable replacement patches,
  selected firewall lists and visitor-header switches reach one transactional
  handler. Selective CLI edits no longer read and merge state before the write
  boundary. Existing shared CRUD services remain the mutation handlers for
  other operations. Python callers now submit explicit commands; delivery and
  storage shapes are unchanged. See decision 0014.

- Canonical managed certificate versions and controller material collection
  implemented: issuer writeback records exact SHA-256 paths in authored policy;
  compilation and rollback resolve those selected bytes independently of latest
  metadata. Input/compiler versions are 3. `cert collect` preserves current
  policy, all retained releases and metadata roots under issuance/write locks.
  Remote edge material remains conservatively retained. See decision 0015.

- Single application distribution implemented: capability code and resources now
  ship in `blitzecdn`; HTTP/3, GeoIP, hardening and resolver are focused runtime
  providers. Composition owns a mandatory roster. Independent extras, workspace
  members, attach/detach modes and package-presence fixture behavior are removed.
  Behavioral and resource contracts remain; isolated installation verifies the
  actual wheel outside the checkout. See decision 0016.

- Explicit schema and installation lifecycle implemented: repository construction
  validates existing state read-only; `migrate` owns initialization/upgrades and
  `setup` also scaffolds credentials. Managed convergence stops services before
  migration. Optional configuration-selection switches are retired. Restore
  retains explicit staged migration. See decision 0017.

- Final architecture review implemented: TLS services belong to the application
  root, global service caches are removed, and failed startup/composition cannot
  bypass owned-resource cleanup. Contract checks cover the complete application;
  current documentation and dependency checks match both registration seams.
  See decision 0018.

## Verified checkpoint

- Strict typing and formatting pass; shell checks and the Ansible production
  lint profile pass.
- Full application suite: 1,766 passed with 91.20% coverage, including actual
  single-wheel installation outside the checkout with locked runtime dependencies.
- Retired core-only/attach/detach checks are replaced by single-distribution,
  mandatory assembly, provider scope and installed-resource contracts. Capability
  and provider behavioral tests run together without package-presence skips.
- One application wheel and source archive build; installed-wheel tests verify
  explicit schema preparation, usable stores and repeatable credential setup.
- Startup and migration tests cover untouched unprepared files, damaged schemas,
  concurrent processes and threads, unusual paths and removed database files.
- Lifecycle regressions cover failed startup, pool creation/draining, shutdown,
  owned/injected persistence and collection of closed TLS service graphs.
- Public frozen snapshots are unchanged by the final architecture review.
- CLI snapshots add `migrate` and remove `setup --schema-only`; Ansible removes
  the retired configuration-selection argument. HTTP, ABI, SDK and database
  snapshots are unchanged.
- Live Ubuntu edge convergence has not been executed and was explicitly deferred
  by the user on 2026-10-24.

## Remaining implementation slices

- Deferred: verify complete convergence, activation, rollback and controller
  recovery on an isolated Ubuntu edge. Resume only when the user asks. Local
  tests do not substitute for this verification.

## Operating and recovery acceptance

The next milestone is a small, understandable system with demonstrated recovery.
Each step requires separate confirmation and its own commit.

1. Complete: inspect existing evidence and document acceptance criteria in
   [RECOVERY_ACCEPTANCE.md](RECOVERY_ACCEPTANCE.md). The selected existing tests
   pass (142 + 29); runtime code and tests are unchanged. Simulated worker
   interruptions and edge activation are explicitly distinguished from real
   process and Ubuntu verification.
2. Complete: [local recovery practice](LOCAL_RECOVERY_PRACTICE.md) killed an
   instrumented worker with SIGKILL, resumed its checkpoint in a fresh process,
   restored a removed controller through the CLI and verified explicit rollback.
   Edge activation remains simulated; Ubuntu verification is still deferred.
3. Complete: correct README authoring syntax and clarify lease owner, deadline
   and takeover guidance in the README and job help. Queue behavior is unchanged.
   Remote Ubuntu 24.04/Python 3.12 validation passed 125 focused tests, format,
   lint, strict types and direct CLI example/inspection checks; see the
   [practice results](LOCAL_RECOVERY_PRACTICE.md).
4. Complete: [retention rules](RETENTION.md) describe cleanup counts, recovery
   copies, exact key preservation and storage budgeting. Finished-check cleanup
   now preserves queued/running deployments and their target rows; the worker
   applies the existing finished-job limit after executing a claim. Remote
   validation passed 293 focused cases, format, lint and strict types; see below.
5. Complete: the [short operating guide](OPERATING_GUIDE.md) covers routine
   inspection, backups, applying/undoing changes, interrupted jobs and rebuilding
   a controller. It distinguishes demonstrated controller behavior from deferred
   real-edge procedures. Remote CLI/restore checks passed; see below.

## Per-step authorization

After each step is tested and independently committed, ask for the user's
confirmation before beginning any further implementation. The user deferred
Ubuntu edge verification. Step 5 was confirmed; any further implementation or
real-edge verification requires its own confirmation.

## Step 4 validation

Validation ran on the user-provided Ubuntu 24.04.5/Python 3.12.3 server in an
isolated temporary source checkout, reusing step 3's locked dependency environment
with `PYTHONPATH` pointing at the new checkout. No tests ran on the workstation.

- Four regression cases first failed against the unchanged implementation:
  cleanup deleted unfinished queued/running checks, and worker ticks never
  enforced the finished-job count for either successful or failed jobs.
- With the fixes, **291 passed and 2 skipped** in the main selection. The two
  real Ansible fixture checks skipped because the dependency environment's
  binaries were absent from the shell `PATH`; both passed when rerun with that
  environment on `PATH`. Thus **293 distinct focused cases passed**.
- Format and lint passed across 486 Python files; strict types passed across
  341 source/helper files. Local documentation links and imports from the
  isolated source were checked remotely.
- Release/certificate retention, rollback, backup/restore, workflow/audit/log
  retention, durable jobs, frozen contracts and process recovery were included.
  This focused run did not measure coverage or replace the earlier full-suite
  checkpoint. Published snapshots and the database schema are unchanged.
- Tests used disposable controller state. No application was deployed, no
  existing controller data was collected, and real Ubuntu edge verification
  remains deferred. No archive rotation, metadata expiration or edge cleanup
  was introduced.

Main selection (using the dependency environment's Python and selecting the
isolated checkout's `src` through `PYTHONPATH`; `pytest` below names that
environment's test runner):

```bash
pytest --no-cov -n auto --dist=worksteal \
  tests/platform/test_repository.py \
  tests/capabilities/jobs tests/capabilities/deployments \
  tests/capabilities/releases/test_release_service.py \
  tests/capabilities/workflows tests/capabilities/backup \
  tests/capabilities/certificates/test_material_lifecycle.py \
  tests/contract/test_ansible.py tests/contract/test_frozen.py tests/journeys
```

The two Ansible fixture checks were then run with the environment on `PATH`:

```bash
pytest --no-cov tests/contract/test_ansible.py -k real_ansible_runner
```

## Step 5 validation

This step changes documentation only. A private temporary source checkout on
the user-provided Ubuntu 24.04.5/Python 3.12.3 server used step 3's dependency
environment with the current source explicitly selected for every CLI process.
The loaded module path was checked; no tests ran on the workstation.

- **36 CLI invocations passed**, including `--help` for 15 command paths in the
  guide. Actual controller commands exercised readiness/history/audit reporting,
  persisted job owner/deadline inspection and lookup of its deployment.
- A full backup of disposable database, TLS and configuration components was
  inspected and restored through the CLI into an empty directory, without setup
  there. Inventory, the original deployment/release identity, API credential bytes
  and generated certificate/private-key bytes survived; the archive and restored
  private key had mode 0600. Temporary controller state was removed afterwards.
- Documentation links and the README lifecycle anchor were checked remotely.
  Deployment/rollback syntax was help-checked here; their controller behavior
  remains supported by the existing journey/CLI tests and step 4's verification.
  No runtime changes required another application-suite run or new tests.
- Real Ansible edge execution, SSH access to an edge, Nginx/HTTPS service, Compose
  restart, host firewall reconciliation and ACME issuance were not performed.
  The guide labels that boundary rather than claiming a production rehearsal.

All five confirmed operating/recovery steps are complete. The user subsequently
authorized the separate real Ubuntu edge rehearsal; its
[preparation record](REAL_EDGE_REHEARSAL.md) documents the current evidence and
remaining acceptance work. Further steps still require individual confirmation.
