# Typed features, two-stage binding, and the extension context

## Context

Every built-in capability registered through pluggy hooks, though none was
optional: the roster was fixed and every entry required. Hooks that needed
collaborators received the whole control plane as `platform` and read whatever
they wanted from it — `platform.certificates`, `platform.deployments`,
`platform.capability_config`. The dependency graph test had to parse those
attribute reads to see which capabilities a hook depended on, and the TLS site
contribution reached the certificate service from inside the release compiler,
which made the compiler's output depend on what a service held. The same
capability used two module names for one seam (`plugin.py`, `provider.py`), and
TLS had both.

## Decision

A built-in declares a `Feature` in `feature.py`: metadata, routers, command
groups, Ansible roles, configuration and Nginx resources, all values. The
composition root names those modules in `BUILTIN_FEATURES`; the registry
imports them by path, which keeps delivery imports out of the root's own
import graph.

Behaviour is bound in two stages, because its collaborators exist at two
different times:

- `FeatureRendering` — site state, fleet state and deployment checks — is bound
  before the release compiler is built, from configuration and adapters only.
  TLS renders from the shared `CertificateStore`, not the certificate service,
  so what a site renders to cannot depend on a service.
- `FeatureOperations` — health checks, scheduled jobs, startup and shutdown —
  is bound after every service exists.

Each lives in the feature's `runtime.py` as a constant or a builder whose
collaborators are named arguments, and the composition root calls it
explicitly. `runtime.py` imports no delivery layer, so the root can import it.
Registering the installation's job schedules consumes every feature's jobs at
once, so `ControlPlane.start` does it, beside the worker's handler table.

External plugins keep pluggy, adapted into the same contributions. A hook that
is not static receives an `ExtensionContext` — process, settings, scoped
configuration, sites, fleet, events and queue readiness — and never the control
plane or a service. That changed every non-static hook's signature, so the
hook API moved to version 2; nothing was deployed against version 1, so it is
not accepted.

## Consequences

No module a capability or provider owns names the composition root, even for
typing; the dependency graph is the import graph again. A built-in's behaviour
is testable by calling its builder with doubles, without a control plane. An
external plugin built against version 1 must rename `platform` to `context` and
read ports from it. Registration order still decides presentation only.

## Code and verification

- `core/plugins/types/feature.py`: `Feature`, `FeatureRendering`,
  `FeatureOperations`, `ExtensionContext`.
- `core/plugins/registry.py`, `discovery.py`, `hooks.py`: declarations,
  binding, external adaptation and name reservation.
- `capabilities/*/feature.py`, `providers/*/feature.py` and their `runtime.py`.
- `composition/control_plane.py`: the roster and both binding stages.
- `tests/architecture/test_plugins.py`: mechanism, binding and failure policy.
- `tests/architecture/test_layering.py`: no module names the composition root;
  runtime modules import no delivery layer; every capability declares a feature.
- `tests/contract/frozen/plugin_abi.txt`, `sdk.txt`: the published contract.
