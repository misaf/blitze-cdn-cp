# 0007: Compose the site policy from feature contributions

Status: Accepted

## Context

A site's serving policy has settings owned by many capabilities: TLS owns
transport and redirects, HTTP owns visitor headers, origins owns origin
request identity, cache owns cache levels, security owns the firewall, and so
on. The shared site model in core must not import capabilities, and a new
per-site setting should not require edits in `sites` or core.

Issue #256 asked whether this composition is worth it for a static, closed
roster. Static declaration would let type checkers see every field; dynamic
composition keeps core independent of capabilities. At the time, composition
happened on an explicit call, so every script, test helper and one-off tool
had to remember it first.

## Decision

`SitePolicy` (`core/domain/site_policy.py`) names no contract. Each capability
declares a `CapabilityPolicy` model in its `contract.py` and contributes it as
a `PolicyContribution` in `Feature.site_policy`. When the registry loads, the
site policy, the zone and rule patches and the `/v3/domains` and `/v3/sites`
schemas are composed from those contributions. A contract is read as
`site.policy[TlsPolicy]`, and a rule that reads another contract declares it
in `depends_on`.

Composition happens when `blitzecdn.composition` is imported:
`BUILTIN_REGISTRY` in `composition/roster.py` is built at module import and
seals `SitePolicy`, so there is no call to remember (#315).

## Consequences

- A new per-site setting touches only its own capability. See
  [where a change belongs](../../README.md#where-a-change-belongs).
- Core stays free of capability imports, which `.importlinter` enforces.
- Type checkers cannot see the composed fields. Code reads a contract through
  `policy[Contract]`, which returns the typed contract.
- Validating a policy before `blitzecdn.composition` has been imported raises
  `FeatureError: the site policy is not composed yet`. See
  [writing a script or a test](../architecture.md#writing-a-script-or-a-test).
- The composed schema is process-global. A test that composes extra
  contributions does so through the `policy_composition` fixture in
  `tests/support/composition.py`, which restores the built-in schema and the
  generated-model caches afterwards.
