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
  tests pass. Atomic staged activation remains to be implemented.

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
  facades. Nested policy composition and shared application commands are next.
  Ownership validation: strict types and formatting pass; 511 architecture,
  package-boundary, policy, DNS, release, and public-contract checks pass with
  13 environment-dependent skips.

- Core-only verification passes: 1,268 passed and 35 skipped with all ten optional
  distributions removed. The static Ansible example compares artifact content
  independently of the installation-specific release digest; publication tests
  assert the exact digest against the stored release. The workspace environment
  was restored after the run.

## Verified checkpoint

- Strict typing and formatting pass; shell checks and the Ansible production
  lint profile pass.
- Full workspace fast suite: 1,837 passed, 13 skipped.
- Core-only suite: 1,268 passed, 35 skipped; workspace packages restored.
- All eleven distributions build as wheels and source archives. The root wheel
  contains the new serving owner and runtime tasks and excludes the retired
  DNS host module.
- Live Ubuntu edge convergence has not been executed.

## Remaining implementation slices

- Stage a complete candidate configuration tree and activate it atomically,
  with explicit reversion on activation or serving-verification failure.
- Replace flat policy inheritance with nested composition and introduce shared
  application commands for API and CLI mutations.
- Bind resource versions to canonical desired state and add reference-aware
  material garbage collection.
- Consolidate independently packaged capabilities into one application with
  focused runtime providers.
- Simplify installation, separate schema migration from ordinary repository
  construction, and verify complete recovery on a real edge.
