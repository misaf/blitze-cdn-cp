# Capability configuration ownership

Status: accepted; documents the design in `core.plugins.types.configuration`,
`core.plugins.resolution.configuration` and `core.config`.

## Context

An optional capability ships as its own distribution and may not be installed.
Anything it asks an operator to configure therefore cannot be a field on core's
`Settings`: that would have the core distribution carry the configuration of a
wheel that may be absent, and offer an operator somewhere to set a value nothing
reads.

Every configurable name lives in one `BLITZE_*` namespace, shared by core and by
whatever capabilities are attached. Ownership of a name has to be decidable, or
a typo, a setting left behind by a detached package, and a package that was
never installed are indistinguishable from a value that is simply being ignored.

## Decision

A capability declares what it asks an operator to configure, through
`blitzecdn_capability_configuration`, as one `ConfigurationContribution` holding
two kinds of claim:

- `EnvironmentKey` — a secret. Core never inspects the value. It stays
  `SecretStr` throughout, is forwarded into Ansible's subprocess environment,
  and never travels through argv or desired state.
- `CapabilitySetting` — a non-secret. Core resolves it to the declared type and
  hands it back. A role needing the same value reads it from the desired-state
  document or from its own role defaults.

One contract rather than two, because "what does this capability need set up" is
one question and `blitzecdn plugins` answers it from one place. Splitting it
would put half the answer on a contract named after a subsystem the other half
never reaches.

### Claims decide ownership

Installation makes a capability available; configuration determines whether it
is used. `Settings.required_capabilities` declares the tokens an installation
needs. Composition checks those tokens against plugin metadata before wiring
services, so detaching a required package fails at startup with the missing
token named. An absent optional package is otherwise supported.

Core stages every non-core `BLITZE_*` name it can see — the process
environment, the controller's `.env`, and `blitzecdn.toml` — and then refuses
any that no installed capability claims. The resolver makes four refusals, each
because the alternative is a much later failure that names nothing useful:

1. A claimed name without the `BLITZE_` prefix: a package claiming a name the
   controller never collects.
2. A name claimed twice: two packages reading one value, either of which may
   reconfigure it for the other's sake.
3. A configured name nobody claims: a typo, a detached package's leftover, or a
   package never installed.
4. A value that cannot be worked with: a required key absent, a secret shorter
   than its declared usable length, or a setting unreadable as its declared
   type.

The first three are `PluginError`: they are all about what the installed
packages claim. The fourth is `ConfigurationError` — nothing is wrong with the
package, and the fix is a value the operator can change. Setting a declared
secret in `blitzecdn.toml` is a `ConfigurationError` for the same reason.

Secrets and settings share the namespace deliberately. They arrive as one set of
names and an operator sets them the same way, so a capability claiming one name
as both, or two capabilities disagreeing about which kind a name is, is the same
collision as any other and is reported as one.

### Sources are not interchangeable

`.env` and the process environment are 0600 and uncommitted; `blitzecdn.toml` is
neither. A setting may come from either, the environment winning. A secret may
come only from the environment, so a package cannot document its signing key
into a file an operator would commit.

### Core validates only what it can without knowing meaning

Presence and a minimum byte length for a secret; type and integer bounds for a
setting. Anything richer stays the package's: core cannot know what a MaxMind
account id or an ACME directory URL looks like, and a core that grew a way to
describe one would again be carrying the shape of a capability that may not be
installed.

`required` is presence, and is not the same question as whether the capability
is useful. A required key is one whose absence makes the installed package
*wrong* rather than idle, and it stops the control plane at startup naming the
key and the package. A signing secret for Under Attack Mode is deliberately not
that: a controller without one is a working controller with one site setting it
will refuse, which is a deployment check's answer.

### Capabilities read their own configuration, scoped

`CapabilityConfig` is the resolved read, done once by core and handed back
scoped to the keys that plugin declared. A package does not reach for
`Settings.capability_environment` itself; that is an untyped `getattr` against a
model core owns, returning every claimed key in the installation, out of which
the package would pick its own by re-spelling the name.

`CapabilitySetting.portable` says whether a value survives a restore onto a
different host. Almost everything does. What must not travel is a value
describing the machine — `blitzecdn-backup`'s archive directory is the standing
case. Core knows which of *its own* settings describe a machine; a capability's
are the capability's to classify.

## Consequences

`blitzecdn.toml` cannot refuse a key core does not recognise, because an
optional capability's settings belong in that file and the reader runs long
before any plugin has said what it claims. The refusal moves to the plugin
resolver, which is the only thing that knows what is claimed.

Every refusal an operator sees can name both the key and the package, including
the case where the package that owned a name has been detached.

Adding a capability setting is a change in that capability's own distribution.
Core carries no capability's name.

## Code and verification

- [Contribution types](../../src/blitzecdn/core/plugins/types/configuration.py)
- [Resolver and refusals](../../src/blitzecdn/core/plugins/resolution/configuration.py)
- [Staging from environment and TOML](../../src/blitzecdn/core/config/loading.py)
- [Settings](../../src/blitzecdn/core/config/settings.py)
- [A capability's own config](../../packages/blitzecdn-certificates/src/blitzecdn_certificates/config.py)
- [Plugin resolution tests](../../tests/architecture/packages/test_package_capability_contracts.py)
