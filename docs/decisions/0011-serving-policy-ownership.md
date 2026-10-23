# Serving owns policy composition and effective virtual hosts

Status: accepted; ownership step of the unreleased application rebuild.

## Decision

DNS owns zones, records, rules, policy authoring, and their projection into hosts.
Serving owns the shared policy vocabulary and the effective virtual-host value.
Compilation, certificates, origin checks, and deployment import those values
directly from serving rather than treating DNS as their domain vocabulary.

Individual capability policies still own their settings. Serving composes them;
origin identity and visitor headers belong to serving itself. A zone consumes
that shared policy and a derived host adds effective hostnames and an origin.

Shared values need no runtime registration. A plugin exists to register commands,
routes, jobs, or execution contributions; it is not required merely because a
module defines a value another module consumes. Architecture checks distinguish
pure domain/policy modules from executable delivery and application modules.

This step changes ownership and imports. The flat policy representation is still
present; nested composition is a subsequent implementation step. No compatibility
facades preserve imports from the old owner, because nothing has been deployed.

## Code and verification

- [Serving policy](../../src/blitzecdn/capabilities/serving/policy/site.py)
- [Effective hosts](../../src/blitzecdn/capabilities/serving/domain/host.py)
- [DNS derivation](../../src/blitzecdn/capabilities/dns/domain/hosts.py)
- [Executable boundaries](../../tests/architecture/test_layering.py)
- [Policy invariants](../../tests/capabilities/serving/test_policy.py)
