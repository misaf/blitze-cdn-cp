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
