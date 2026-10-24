# Nested serving policy values

## Context

`SitePolicy` inherited seven capability models into a flat namespace. Zones and
virtual hosts then inherited that aggregate. A virtual host was consequently a
TLS policy, a cache policy and several other unrelated things at once, and
capability discovery called properties found by walking its method-resolution
order. The persisted representation and the delivery representation were also
the same shape, making internal ownership a published interface concern.

## Decision

`SitePolicy` is a frozen value with named `tls`, `protocols`, `origin`, `cache`,
`compression`, `security` and `headers` fields, plus the shared `enabled` flag.
Each component is the pure typed contract owned by its capability. `Domain`
and `CdnSite` each **contain** a policy; neither inherits policy settings.
Consumers read the owning value explicitly, for example
`site.policy.tls.ssl_mode` or `site.policy.security.firewall`.

Capability requirements come from the contained values. A typed field declares
a component, so there is no second registry or inheritance walk. The editing
vocabulary must have exactly one owner for every setting: duplicate owners or
fields that are not policy contracts are programmer errors and are refused.
Cross-capability rules such as HTTP/3 requiring TLS remain on the composing
policy, while site-specific managed certificate paths remain on the host.

Canonical storage serializes the nested values directly. The zone row stores
`policy.model_dump(mode="json")`; release inputs include the zone's named
policy value. Input schema and compiler versions advance to 2. Retired input
versions are rejected explicitly. There is no upcaster, dual storage shape,
flat constructor alias or automatic validation shim: this application has no
deployed compatibility obligation.

## Delivery and editing boundaries

API and CLI settings and the edge roles keep their flat field vocabulary.
`SitePolicy.from_settings` is an explicit conversion at that boundary;
`settings` projects the value back. Ordinary model construction and JSON
validation accept the nested representation only. The DNS delivery adapter
shares that projection between HTTP responses, CLI output and Ansible mapping.
Ansible still omits nulls and blocks declaring `OmittedWhenEmpty`; visitor-header
defaults are retained.

`DomainPatch` and rule overrides remain flat editing commands. Their field and
type parity check compares against the composed contracts' setting definitions,
not against the aggregate's storage fields. `SitePolicy.applying` revalidates
named settings while retaining unrelated values. Firewall and visitor-header
blocks still replace wholesale when named, and the first matching rule still
supplies the complete set of overrides. These semantics are unchanged by the
addition of capability containers.

Certificate writeback, automatic TLS probing/upgrades, cache purge, serving
verification, plugins and origin checks use the nested values. Automatic TLS
probe candidates are reconstructed and validated with their updated policy,
rather than installing an unvalidated flat key with `model_copy`.

Shared application commands are separate subsequent work. This decision does
not merge delivery handlers or change packaging and provider lifecycle.

## Code and verification

- [Composition](../../src/blitzecdn/capabilities/serving/policy/site.py)
- [Zone value](../../src/blitzecdn/capabilities/dns/domain/zone.py) and
  [host value](../../src/blitzecdn/capabilities/serving/domain/host.py)
- [Delivery projection](../../src/blitzecdn/capabilities/dns/adapters/documents.py)
  and [Ansible omission](../../src/blitzecdn/capabilities/dns/adapters/ansible.py)
- [Patch parity](../../src/blitzecdn/capabilities/dns/domain/patch.py), resolution,
  host derivation and certificate writeback
- [Composition invariants](../../tests/capabilities/serving/test_policy.py):
  nested-only storage, frozen values, contained capability requirements,
  duplicate ownership refusal and patch retention/block replacement
- [Release integrity](../../tests/capabilities/releases/test_integrity.py):
  canonical nested policy roundtrip and explicit retirement of schema 1
- Existing API, CLI, rule, persistence, certificate, origin and rendered-edge
  regressions verify the boundaries. Published surface snapshots are unchanged.
