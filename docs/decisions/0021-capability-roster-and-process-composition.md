# One capability roster, deferred extension platform, per-process composition

Status: accepted. Amends [0016](0016-single-application-and-runtime-providers.md)
and [0019](0019-typed-features-and-extension-context.md).

## Context

Built-in features are already typed values (0019): `BUILTIN_FEATURES` names
their modules and the registry imports them. Two further mechanisms sit around
that roster, and both cost more than they currently return.

**An external extension platform with no consumers.** External plugins enter
through the `blitzecdn.plugins` entry-point group and answer the pluggy hooks
in `core/plugins/hooks.py`. Supporting them takes hook API version 2,
`SUPPORTED_HOOK_API_VERSIONS`, `ExtensionContext`, rejection reporting, merging
external results into every registry answer, the fixtures under
`tests/external_plugins/`, and two frozen contracts: `plugin_abi.txt` and
`sdk.txt`. No external plugin exists and nothing is deployed. The application
is meanwhile reimplementing deployments, jobs and releases, so every hook the
ABI freezes today describes internals that are still expected to change.

The SDK also publishes the composition root itself:
`api.dependencies.get_control_plane` and `cli.common.control_plane` both
return `ControlPlane`. A frozen public surface whose type is "every service in
the application" constrains the composition root, not just extensions.

**One feature named in three places.** A built-in appears in
`BUILTIN_FEATURES` by module path, again in the rendering tuple passed to
`bind_rendering`, and again in the operations tuple passed to
`bind_operations`. `_check_bound` rejects a mismatch at startup; nothing
rejects it earlier, and adding a feature means editing all three.

**One composition object for every process.** `ControlPlane` builds every
adapter and service for the API, the worker, the CLI and the install handoff
alike. It has around 35 public attributes and 10 optional constructor
arguments that exist for tests, and it branches on `ProcessKind` (worker
handlers, schedule registration, and a check in `deployments/runtime.py`).
Route handlers and 19 CLI modules reach services through it, so any of them
can reach any service, and the composition root is effectively a service
locator for delivery code.

## Decision

### Keep the seam, defer the platform

The extension point that stays is a typed capability roster. The machinery for
loading, versioning and isolating third-party code is removed until a real
consumer needs it.

`composition/roster.py` holds `ROSTER`, one `RosterEntry` per feature module:

```python
@dataclass(frozen=True, slots=True)
class RosterEntry:
    module: str                                       # its feature.py, by path
    rendering: Callable[[RenderingInputs], FeatureRendering] | None = None
    operations: Callable[[OperationInputs], FeatureOperations] | None = None
```

The composition root binds rendering and operations by iterating that table,
so the three-place registration becomes one row, and a feature without
behaviour leaves both builders unset.

The module stays a path rather than an imported value. `feature.py` imports
its routers and command groups, and those delivery modules import the
composition root, so importing feature modules from the root would be a cycle.
This is the constraint 0019 recorded, and it still holds.

The two binding stages from 0019 remain, because their collaborators still
exist at different times. Each stage's builder receives one record:

- `RenderingInputs`: scoped capability configuration and the adapters
  rendering may read (`CertificateStore`). No services.
- `OperationInputs`: settings and the services operations call.

Both records are defined in the composition package and never reach a
capability. Each row's builder is a short function that reads what it needs
from the record and calls the capability's `runtime.py` builder with named
arguments, so 0019's property is unchanged: a capability's behaviour is
testable by calling its builder with doubles.

Removed: pluggy and its hook specifications, `HOOK_API_VERSION` and supported
versions, `PluginMetadata.api_version` and `required` (every feature is built
in and required), `ExtensionContext`, external discovery and rejection
reporting, `tests/external_plugins/`, and the frozen `plugin_abi.txt` and
`sdk.txt`. `blitzecdn plugins` lists the roster's metadata.

With no plugin platform left, the mechanism is named for what it holds.
`core/plugins/` is `core/features/`; `PluginRegistry`, `PluginMetadata` and
`PluginError` are `FeatureRegistry`, `FeatureMetadata` and `FeatureError`; the
`plugin` field on every contribution is `feature`; and a registry's metadata
is `registry.metadata`. Three names that leave the controller keep the old
word, because renaming them changes a contract rather than a name: the
`blitzecdn plugins` command (frozen CLI surface), the `plugin` key of
`blitzecdn_nginx_modules` (frozen Ansible surface), and the
`blitzecdn-plugin-*` Nginx file names already written to edges.

### When the platform comes back

Third-party extension is reintroduced when at least one of these is true: a
named partner or customer will write a plugin within the next release or two,
capabilities are to be distributed separately, or another team ships
capabilities on its own release cadence.

It then returns in this order, each step being additive:

1. An entry-point loader that appends external entries to the same roster,
   with name collisions refused. This is the only step needed for a trusted
   in-house plugin.
2. A versioned public contract covering `Feature`, the contribution types, the
   inputs an external entry may receive and the domain events plugins may
   consume. It is frozen only after the reimplementation sequence in
   `docs/REIMPLEMENTATION.md` is complete.
3. Isolation and compatibility reporting, if untrusted plugins must load.

The composition root, the contribution types and the delivery layers do not
change for any of these steps.

### Composition per process

`ControlPlane` is replaced by three layers:

- `Infrastructure` (`composition/infrastructure.py`): the concrete adapters,
  meaning the repository and its stores, the fleet runner, the probes, the
  job queue and scheduler, and the resolved contributions they need.
  `Infrastructure.build(settings)` builds the real ones. A test passes a double
  for the adapter it is about (`runner=`, `serving_probe=`, `repository=`).
  Overrides exist only here, on adapters, never on services. `close()` releases
  what it opened.
- `ApplicationServices` (`composition/services.py`): every service, built once
  from an `Infrastructure` by `build_services`. It has no lifecycle and no
  process branches; it is the one definition of the service graph.
- One container per process (`composition/processes.py`), each built from the
  service graph and holding only what that process uses:
  - `build_api(services) -> ApiServices`
  - `build_worker(services) -> WorkerServices`, which installs the job handlers
  - `build_cli(services) -> CliServices`
  - `build_install_handoff(services) -> HandoffServices`

`ProcessKind` disappears: a process's differences are which container it
builds, not branches inside one object. `ApiServices.start` runs feature
startup (deployment recovery, once per node) and registers schedules;
`WorkerServices.start` registers schedules. Both stop feature operations on
shutdown. Every container closes the infrastructure it was built on.

Delivery code depends on its own container. Routes read `ApiServices` through
a FastAPI dependency; CLI commands read `CliServices`. Neither can reach a
service its process was not given.

## Consequences

- Adding a capability is its feature module and one row in the roster.
- The core plugin package shrinks to the contribution types and resolution
  (Ansible, Nginx, configuration), which are still needed for built-ins.
- Removing `plugin_abi.txt` and `sdk.txt` is an intentional frozen-surface
  change. The CLI, HTTP, Ansible and schema surfaces do not change.
- Capability `runtime.py` builders keep their named arguments; only the
  roster rows read the input records.
- `FeatureMetadata` loses `api_version` and `required`, and a feature's
  `startup` and `shutdown` take no process argument.
- Tests built on `ControlPlane` build `ApplicationServices` over an
  `Infrastructure` with doubles, and wrap it in a process container when the
  test is about that process. Wiring-only tests for external hooks are deleted
  with the hooks.
- The cost of reopening the platform later is the loader in step 1 above, not
  a redesign, because the roster is already the single point every consumer
  reads.

## Code and verification

This record governs:

- `composition/roster.py`: `ROSTER`, `RenderingInputs`, `OperationInputs`.
- `composition/infrastructure.py`, `services.py` and `processes.py`, which
  replace `composition/control_plane.py`.
- `core/features/`: `hooks.py` and `manager.py` removed; `discovery.py` keeps
  only built-in loading; `registry.py` loses its external half; `types/` kept.
- `capabilities/*/feature.py`, `providers/*/feature.py`: metadata without
  `api_version` or `required`. `runtime.py` builders keep named arguments.
- `api/dependencies.py`, `cli/common.py`, `worker.py`, `install_handoff.py`:
  use their process containers.
- `tests/architecture/test_features.py`: roster uniqueness, that every builder
  binds, and that both stages run in the right order.
- `tests/architecture/test_layering.py`: each entry point builds only its own
  process container, and delivery code names no part of composition except its
  own container (`test_each_process_builds_only_its_own_container`,
  `test_delivery_code_reaches_only_its_own_process_container`).
- `tests/contract/frozen/`: `plugin_abi.txt` and `sdk.txt` removed. The CLI,
  HTTP, Ansible and schema snapshots are unchanged; `blitzecdn plugins` drops
  its `required` and `rejected` fields, which no snapshot records.
