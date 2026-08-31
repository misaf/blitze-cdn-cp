# Plugins

BlitzeCDN is a package-by-feature modular monolith whose features register
themselves. `pluggy` is the mechanism. This page is the contract: what it is
used for, what it must never be used for, and how to add a feature — inside this
repository or as a package installed beside it.

## Core versus features

**Core** (`src/blitzecdn/core/`) is what a feature is allowed to build on:
configuration, persistence and its transaction boundary, the event and audit
journals, the workflow coordinator, the queue, Ansible execution, the filesystem
and process adapters, and the plugin infrastructure itself. Core owns no
business capability. `tests/test_layering.py` fails a `core` module that imports
a feature's `service` or `adapters`.

**Features** (`src/blitzecdn/features/`) are independently meaningful business
or operational capabilities: `sites`, `dns`, `certificates`, `automatic_ssl`,
`cache`, `deployments`, `edges`, `backup`, `diagnostics`, `maintenance`. A
feature owns the layers its actual behavior needs and the `plugin.py` that tells
the control plane it exists. Small features need not invent service, repository,
or adapter layers merely to match a directory template.

Individual CDN switches are not features. TLS, HTTP protocols, compression,
cache policy, request security, visitor headers, and origin behavior are
cohesive modules under `features/sites/policy/`. Operational cache purge and
statistics remain in `features/cache`; certificate issuance and storage remain
in `features/certificates`; runtime/build capability remains an edge concern.

**`bootstrap.py`** is the composition root, and the only place production wiring
lives. It builds adapters, injects them into services through constructors,
loads the plugins, and hands each plugin the finished control plane so it can
register what it contributes.

```
adapters  →  services  →  plugins  →  contributions
```

Nothing flows the other way.

## What pluggy is for

Registration, and only registration:

* feature discovery and registration
* API router registration
* CLI command registration
* scheduled jobs
* health checks
* desired-state contributions
* deployment validation
* startup and shutdown lifecycle work

## What pluggy is not for

Business communication. A caller that wants work done calls the service that
owns it, through a constructor-injected reference:

```python
certificate_service.issue(...)  # yes
deployment_service.deploy(...)  # yes
dns_service.create_record(...)  # yes

plugin_manager.hook.issue_certificate(...)  # no
```

A service call has one implementation, a return value, a type, and a stack trace
that names it. A hook call has none of those. Issuing a certificate is something
exactly one service does, so it is not an extension point.

There is no global plugin manager, no `get_service()`, and no registry lookup in
a request path. `platform.cache` appears in a `plugin.py` and nowhere else —
a typed attribute read once, at registration.

## The hooks

Ten, all prefixed `blitzecdn_`. A hook whose contribution is static takes no
arguments; every other hook takes `platform`, the built control plane.

| hook | arguments | returns |
| --- | --- | --- |
| `blitzecdn_plugin_metadata` | – | `PluginMetadata` (required of every plugin) |
| `blitzecdn_api_routers` | – | `Sequence[APIRouter]` |
| `blitzecdn_cli_commands` | – | `Sequence[CliCommandGroup]` |
| `blitzecdn_health_checks` | `platform` | `Sequence[HealthCheck]` |
| `blitzecdn_scheduled_jobs` | `platform` | `Sequence[ScheduledJob]` |
| `blitzecdn_site_desired_state` | `site`, `platform` | `SiteStateContribution \| None` |
| `blitzecdn_fleet_desired_state` | `sites`, `platform` | `FleetStateContribution \| None` |
| `blitzecdn_deployment_checks` | `site`, `platform` | `Sequence[ValidationIssue]` |
| `blitzecdn_startup` | `context`, `platform` | `None` |
| `blitzecdn_shutdown` | `context`, `platform` | `None` |

A plugin implements only the hooks it has something to say through. `backup`
contributes commands and no routes; `sites` contributes the site document and
the fleet variables derived from site protocol policy.

`RuntimeContext.process` is `api`, `cli`, `worker` or `scheduler`. It exists
because what a process owes at startup differs: republishing queued deployments
takes the fleet-wide deployment lock and belongs to the API alone.

Lifecycle hooks promise no ordering. A plugin whose startup depends on another
plugin's startup has a dependency it cannot declare here; that belongs in the
composition root, where the two services are built in an order you can read.

## Desired state

Deployment rendering is contributed, not centralised. `DesiredStateRenderer`
knows how a document is framed — site documents under one key, fleet variables
beside them — and nothing about what goes in one:

* `sites` contributes the base site document, projected from `CdnSite`
* `certificates` contributes the TLS paths, and **overrides** the two the model
  projected, because only this controller knows the fingerprinted filenames
* `sites` projects HTTP/3 policy into the fleet-wide QUIC switch and the single
  Nginx listener owner required by `reuseport`

This projection does not make site policy an edge capability. A site may ask
for Brotli or HTTP/3; deployment validation and the edge roles remain
responsible for proving that the image, Nginx modules, runtime, and firewall can
honor that request.

Contributions are typed (`SiteStateContribution`, `FleetStateContribution`) and
merged order-independently:

* two plugins writing one variable is a `PluginError`, not a race
* unless exactly one of them declares the variable in `overrides`
* two plugins both claiming an override is also a `PluginError`

So `BUILTIN_PLUGINS` can be reordered freely and no edge converges differently.
Never concatenate configuration text through a hook — contribute typed values
and let the edge roles render them.

## Adding a built-in feature

1. Create `src/blitzecdn/features/<name>/` with only the domain, service, ports,
   and entry adapters its existing responsibilities require. Keep it small — do
   not build a `domain/application/infrastructure` tree for four functions.
2. Write `plugin.py` with `blitzecdn_plugin_metadata` (`required=True`) and the
   hooks it contributes through.
3. Add the module path to `BUILTIN_PLUGINS` in `core/plugins/discovery.py`.
4. Build the service in `bootstrap.py` with explicit constructor injection.
5. Add the feature to `ALLOWED_FEATURE_DEPENDENCIES` in
   `tests/test_layering.py`, declaring every other feature it depends on.

Built-ins are an explicit tuple rather than entry points on purpose: the control
plane *is* these features, so resolving them through installation metadata would
turn a broken editable install into a node that starts happily and serves an
empty fleet.

## Adding an external plugin

A separately installable distribution — `blitzecdn-waf`, `blitzecdn-geoip`,
`blitzecdn-monitoring` — declares itself in the entry-point group:

```toml
[project.entry-points."blitzecdn.plugins"]
waf = "blitzecdn_waf.plugin"
```

and ships a module of `@hookimpl` functions:

```python
from blitzecdn.core.plugins import PluginMetadata, ScheduledJob, hookimpl


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(name="waf", version="1.2.0", summary="Rule enforcement.")


@hookimpl
def blitzecdn_api_routers():
    return (router,)


@hookimpl
def blitzecdn_scheduled_jobs(platform):
    return (ScheduledJob(name="waf-rule-refresh", interval_seconds=900, run=refresh),)
```

Nothing in this repository changes. A working example lives in
`tests/external_plugins/external.py` and is exercised through the real
entry-point machinery by `tests/test_plugins.py`.

A scheduled job needs no actor: the scheduler publishes the job's *name* and the
worker resolves it against the registry in its own process.

## Failure policy

One rule, one input — `PluginMetadata.required`:

| failure | required | optional |
| --- | --- | --- |
| import raises | `PluginError`, startup stops | logged, skipped |
| registration raises | `PluginError` | logged, skipped |
| no metadata hook | `PluginError` | logged, skipped |
| duplicate name | `PluginError` either way | `PluginError` either way |
| `api_version` mismatch | `PluginError` | logged, skipped |
| a hook returns the wrong type | `PluginError` naming the type | same |

A built-in is required by definition, so any failure there is fatal. Nothing is
ever ignored silently: every skip is logged with the plugin that caused it and
kept on `registry.rejected`, so an operator can be told why the package they
installed is not running. Built-ins register first, so an external plugin
claiming a built-in's name collides with it rather than displacing it.

## Dependency rules

* A feature may depend on core contracts, on ports it declares itself, and on
  another feature's public contract modules (`domain`, `policy`, `origins`,
  `ports`, `reporting`, `snapshots`). `sites` depends on no other feature; DNS
  and operational features may depend on public `sites` contracts.
* A feature may never import another feature's adapters, persistence, internal
  models, or private helpers.
* Cross-feature edges are declared in `ALLOWED_FEATURE_DEPENDENCIES` and
  enforced in both directions; the graph must stay acyclic.
* `platform.<service>` in a `plugin.py` counts as a dependency and must be
  declared in that graph like any import.
* Only `plugin.py` may name the composition root, and only under
  `TYPE_CHECKING`. A service, domain module or adapter that reached for it would
  be resolving collaborators instead of receiving them.
* `api/app.py` and `cli/main.py` import no feature at all.

Every one of these is a test in `tests/test_layering.py`, not a convention.
