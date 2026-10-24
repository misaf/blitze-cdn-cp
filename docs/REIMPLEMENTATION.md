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
- Full application suite: 1,764 passed with 91.17% coverage, including actual
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

## Per-step authorization

After each step is tested and independently committed, ask for the user's
confirmation before beginning any further implementation. The user deferred
Ubuntu verification and authorized the final architecture review instead.
