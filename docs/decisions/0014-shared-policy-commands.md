# Shared policy commands at the application boundary

## Context

API and CLI mutations already use application services for zone, record, rule,
edge and deployment operations. Two policy edits still assembled their result
in delivery code: firewall list edits and visitor-header switches read the
zone, merged a leaf block and submitted a replacement patch. A competing write
between that read and the service transaction could silently lose an unnamed
list or switch. Reading the zone again inside the service did not repair a
replacement block already built from stale state.

## Decision

`DnsService.update_domain` accepts an immutable `PolicyCommand`. There are three
explicit intentions:

- `PatchPolicy` replaces named settings, including whole leaf blocks. It captures
  a validated `DomainPatch` as canonical JSON, preserving explicit nulls and
  detaching mutable input. Each resolution returns an independent working copy.
- `EditFirewall` replaces selected rule lists or explicitly clears all lists.
  Tuple fields capture immutable intent. Unnamed lists keep their current values.
- `EditVisitorHeaders` changes selected switches and keeps unnamed switches.

API PATCH and ordinary CLI setting commands construct `PatchPolicy`. The two
selective CLI commands construct their corresponding intent values without
reading the zone. This retains the deliberately different block replacement
semantics recorded in decision 0004.

The existing application handler owns the entire operation: enter the unit of
work, read the current zone, resolve intent, enforce certificate ownership,
validate the composed policy, replace against the value read, and record the
audit event. A validation or audit failure leaves both policy and audit unchanged.

Command values live in the pure DNS domain contract; state access remains in
the service. There is no dispatcher, handler registry or second application
layer. Existing shared CRUD services remain the application boundaries for
other mutations. Their typed inputs already express complete writes and need
no additional forwarding wrappers. A future selective edit must resolve its
intent inside the owning transaction rather than performing delivery-side
read/merge/write.

## Consequences and verification

The Python update method now requires a command instead of `DomainPatch`;
callers convert explicitly. There is no compatibility overload. HTTP, CLI,
Ansible and storage shapes are unchanged, and policy schema/compiler versions
remain 2 because canonical desired state has not changed.

Regression tests cover immutable capture, absent versus null settings,
transaction-entry competing edits for both selective commands, invalid merged
country rules, audit failure rollback, whole-block replacement, and CLI intent
submission without a preliminary read. Existing API, CLI, certificate and
release tests exercise the migrated call sites.
