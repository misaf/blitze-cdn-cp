# Typed features, two-stage binding, and the extension context

Amended by [0021](0021-capability-roster-and-process-composition.md): the
external pluggy path, `ExtensionContext` and the hook ABI are removed, and the
roster now carries each feature's rendering and operations builders. Typed
features and the two binding stages below are unchanged.

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
collaborators are named arguments. The composition root's roster row for the
feature module calls it (0021). `runtime.py` imports no delivery layer, so the root can import it.
Registering the installation's job schedules consumes every feature's jobs at
once, so the API's and the worker's process containers do it on start (0021).

There are no external plugins (0021).

## Consequences

No module a capability or provider owns names the composition root, even for
typing; the dependency graph is the import graph again. A built-in's behaviour
is testable by calling its builder with doubles, without the service graph.
Registration order still decides presentation only.

## Code and verification

- `core/features/types/feature.py`: `Feature`, `FeatureRendering`,
  `FeatureOperations`.
- `core/features/registry.py`, `discovery.py`: declarations, loading and
  binding.
- `capabilities/*/feature.py`, `providers/*/feature.py` and their `runtime.py`.
- `composition/roster.py`: the roster and both binding stages.
- `tests/architecture/test_features.py`: mechanism, binding and failure policy.
- `tests/architecture/test_layering.py`: no module names the composition root;
  runtime modules import no delivery layer; every capability declares a feature.
