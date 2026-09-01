# Plugins

BlitzeCDN is a package-by-feature modular monolith whose features register
themselves. `pluggy` is the mechanism. This page is the contract: what it is
used for, what it must never be used for, and how to add a capability — as a
required part of the control plane, or as a distribution installed beside it.

## Three categories, and the difference between them

Everything in this repository is exactly one of these, and the distinction is
load-bearing rather than descriptive:

| | where it lives | how it registers | can it be absent? |
| --- | --- | --- | --- |
| **1. Core** | `src/blitzecdn/core/` | it *is* the control plane | no |
| **2. Built-in required capabilities** | `src/blitzecdn/features/` | `BUILTIN_PLUGINS` | no — a failure is fatal |
| **3. Installable optional capabilities** | `packages/blitzecdn-*/` | the `blitzecdn.plugins` entry-point group | yes, and that is normal |

The third category is a real Python distribution. The official optional wheels
are `blitzecdn-backup`, `blitzecdn-cache`, `blitzecdn-compression`,
`blitzecdn-certificates`, `blitzecdn-security`, `blitzecdn-http3` and
`blitzecdn-geoip`. They install beside the
control plane and are found through their installed metadata:

```
install the package  →  pluggy discovers the feature  →  the capability exists
remove the package   →  pluggy discovers nothing      →  core still works
```

No line of core is edited either way. Nothing in `blitzecdn` imports an optional
distribution, and no feature-specific installation check exists.
`tests/architecture/test_packages.py` enforces that direction;
`tests/architecture/test_lifecycle.py` builds real wheels and asserts attachment
and detachment in throwaway virtualenvs.

### What is *not* a package

A **strategy**, a **protocol version**, a **mode** and a **single switch** stay
inside the capability that owns them, and no amount of "it can be turned off"
changes that:

```
capability
└── feature internals
    └── strategy / mode / option
```

gzip and Brotli are values of `CompressionMode` inside `blitzecdn-compression`, not
`blitzecdn-gzip` and `blitzecdn-brotli`. HTTP/1.1, HTTP/2 and HTTP/3 are
versions of one protocol, and the `http` capability owns the contract for all
three — but the *implementation* behind the HTTP/3 switch is separable and
ships as `blitzecdn-http3`. There is no `blitzecdn-http1` or `blitzecdn-http2`:
baseline HTTP is not something an operator attaches. Under Attack Mode is a switch on
`blitzecdn-security`. The minimum TLS version, the cache TTL, the visitor-IP header and
the origin SNI option are fields on a site.

The test for a package is not "can this be disabled" but:

> Can BlitzeCDN operate coherently with this capability *not installed*, and
> does removing it remove meaningful implementation, dependencies, registration
> or operational behavior?

### Why site-policy contracts stay in core

`CompressionPolicy`, `SecurityPolicy`, and `TlsPolicy` cannot be extracted, and
the reason is worth writing down because it will come up again. `CdnSite`
composes `CompressionPolicy`, `ProtocolPolicy`, `SecurityPolicy` and `TlsPolicy`
**by inheritance** into one flat, frozen model, and that model is consumed by
the v1 and v2 HTTP schemas, by persisted policy JSON, by the versioned
deployment snapshots, and by every edge role. Assembling it from whichever
plugins happen to be installed would make the published schema and the on-disk
snapshot depend on the installation, and would leave Ansible parsing a desired
state whose shape it cannot predict.

The contracts therefore stay. Their implementation can still leave:
`blitzecdn-compression` supplies gzip/Brotli capability,
`blitzecdn-certificates` owns managed certificate operations and Automatic SSL,
`blitzecdn-security` owns site-level security deployment validation,
`blitzecdn-http3` owns the QUIC listener the `http3_enabled` switch asks for,
and `blitzecdn-geoip` owns the visitor country lookup that the `BZ-IPCountry`
header and the two firewall country lists both ask for.
Core can deserialize every site without them and then report an unavailable
capability deterministically.

`ProtocolPolicy` is the clearest case of why the field and the implementation
part company. `http3_enabled` stays in core precisely *so that* a controller
without `blitzecdn-http3` can still read back a site that asks for HTTP/3 — and
having read it, refuse to deploy it by name. A contract that travelled with the
package would turn a detached capability into an unreadable database row.

A contract that stays, though, keeps its own *vocabulary*. `SiteFirewall` is
validated against the ISO 3166-1 alpha-2 table, the HTTP-method shape and the
alias table that turns `UK` into "use `GB`", and all four lived in
`core/validation.py` — a module for primitives two or more capabilities share.
They had one consumer, so core was carrying the words of a capability an
operator can detach. They now sit beside the rules that use them, and
`test_core_knows_no_kind_of_firewall_rule` holds `core/` to naming no rule kind
and no part of that vocabulary.

The same rule reached the adapter. `site_to_ansible` used to read `if
site.firewall.empty: del document["firewall"]`, which is one capability's block
named inside generic infrastructure, and a second such block would have been a
second branch there. A block now opts in by subclassing
`core.validation.OmittedWhenEmpty`, and core prunes whatever declares it
without asking what it holds — deliberately *not* every nested block, because
`visitor_headers` carries switches whose default the role reads, so "never
configured" and "switched off" are not the same document.

## Core versus features

**Core** (`src/blitzecdn/core/`) is what a feature is allowed to build on:
configuration, persistence and its transaction boundary, the event and audit
journals, the workflow coordinator, the queue, Ansible execution, the filesystem
and process adapters, and the plugin infrastructure itself. Core owns no
business capability. `tests/architecture/test_layering.py` fails a `core` module that imports
a feature's `service` or `adapters`.

**Features** (`src/blitzecdn/features/`) contain required capabilities and stable
site contracts: `sites`, `dns`, `http`, `tls`, `compression`, `security`,
`deployments`, `edges`, `diagnostics`, `maintenance`. A contract-only directory
does not have a built-in plugin. Small features need not invent service,
repository, or adapter layers merely to match a directory template.

**Optional capabilities** (`packages/`) are the same shape one directory out,
and are covered in [Installable optional capabilities](#installable-optional-capabilities).

One top-level package is one capability. Certificate issuance and the Automatic
SSL/TLS scan are two parts of `blitzecdn-certificates`, not separate packages.
`tests/architecture/test_layering.py` refuses a top-level `features/gzip`,
`features/http3`, `features/certificates` or `features/under_attack` package by
name — a *feature directory* named after a strategy, a version or a mode is
always wrong. An optional distribution is a different question: it is named
after the implementation an operator attaches or removes, which is why
`blitzecdn-certificates` and `blitzecdn-http3` exist as wheels while
`features/certificates` and `features/http3` remain refused.

Each capability splits in two:

* **`policy.py` — the contract.** Pure values describing how the capability is
  configured. It imports `core` and other capabilities' contracts, and nothing
  else. `sites` composes every contract into the one flat `CdnSite`.
* **everything else — the implementation.** Services, adapters, routers,
  commands. These consume `CdnSite`, so they sit *above* the contract layer.

That split is why the layering test declares two graphs. Counting a contract
edge and an implementation edge as one kind would make `sites` ↔ `tls` a cycle
and force every setting back into `sites`, which is the shape this replaced.
The rule that keeps the layers ordered is asserted directly: a contract never
imports an implementation.

Cache *policy* — a site's TTLs and its query-string mode — is `CachePolicy`
under `sites/policy/`, because nothing outside a site's own configuration reads
it. Cache *operations* — purging, and reading how well the cache is working —
are the `blitzecdn-cache` distribution. Edge runtime and build capability
remains an edge concern.

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

A plugin implements only the hooks it has something to say through.
`blitzecdn-compression` contributes neither routes nor commands and that is not an error;
the installed `blitzecdn-backup` contributes commands and no routes; `sites`
contributes the site document and the fleet variables derived from site protocol
policy.

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
* `blitzecdn-certificates` contributes managed certificate source/destination
  paths, and **overrides** the two the model projected, because only the
  certificate store knows the fingerprinted filenames
* `http` projects HTTP/3 policy into the fleet-wide QUIC switch and the single
  Nginx listener owner required by `reuseport`
* `blitzecdn-security` contributes no state, but refuses a deployment whose site asks for
  Under Attack Mode on a controller with no challenge secret
* `blitzecdn-geoip` contributes no state at all: whether an edge resolves
  countries is fleet Ansible policy an operator sets, not something the control
  plane derives, so the document is identical whether or not it is installed

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

## Adding a built-in required capability

First answer: should this be optional instead? A capability BlitzeCDN can
operate coherently without belongs in `packages/`, not here. Then:

1. Create `src/blitzecdn/features/<name>/` with only the domain, service, ports,
   and entry adapters its existing responsibilities require. Keep it small — do
   not build a `domain/application/infrastructure` tree for four functions.
2. Write `plugin.py` with `blitzecdn_plugin_metadata` (`required=True`) and the
   hooks it contributes through.
3. Add the module path to `BUILTIN_PLUGINS` in `core/plugins/discovery.py`.
4. Build the service in `bootstrap.py` with explicit constructor injection.
5. Add the feature to `ALLOWED_FEATURE_DEPENDENCIES` in
   `tests/architecture/test_layering.py`, declaring every other feature it
   depends on — and to `ALLOWED_POLICY_DEPENDENCIES` if it has a `policy.py`
   contract or composes another capability's.

Before step 1, answer the question the structure exists to make obvious:
**which existing capability owns this?** A new top-level feature is the
exception. An option, a strategy, an encoding, a protocol version or a mode
belongs inside the capability that already owns its behavior.

Built-ins are an explicit tuple rather than entry points on purpose: the control
plane *is* these features, so resolving them through installation metadata would
turn a broken editable install into a node that starts happily and serves an
empty fleet.

## Installable optional capabilities

An optional capability is an ordinary Python distribution in `packages/`, a
member of the uv workspace, resolved from the same `uv.lock` and built as its
own wheel:

```
packages/blitzecdn-cache/
├── pyproject.toml
├── README.md
├── src/blitzecdn_cache/
│   ├── __init__.py
│   ├── plugin.py          # the hooks — the only module the entry point names
│   ├── composition.py     # this package's own composition root
│   ├── domain.py  policy.py  ports.py  service.py  adapters/  api/  cli.py
└── tests/                 # its tests, which travel with it
```

### How it registers

The installed metadata, and nothing else:

```toml
[project]
name = "blitzecdn-cache"
version = "3.0.0"
requires-python = ">=3.12"
dependencies = ["blitzecdn>=3.0.0,<4"]

[project.entry-points."blitzecdn.plugins"]
cache = "blitzecdn_cache.plugin"
```

The group is the one external plugins have always used — `blitzecdn-waf` from
outside this repository declares itself the same way — which is what makes the
extracted case and the third-party case the same case. There is no second
registry, no import list in core, no filesystem scan and no `sys.path`
manipulation.

`required=False` in the metadata is the failure policy that goes with it: a
broken optional package is reported by name and skipped, and the node still
serves. `provides` is the set of capability *tokens* the package supplies, which
is what configuration depends on; it defaults to the plugin's own name.

```python
@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="cache",
        version=__version__,
        required=False,
        provides=frozenset({"cache"}),
        summary="Purge cached responses and read cache effectiveness.",
    )
```

### How it declares dependencies

One dependency, pointing inward, with an explicit compatibility range. The
upper bound is not decoration: `HOOK_API_VERSION` may only move in a major, and
a plugin written against v1 that installed beside a v2 control plane would be
refused at registration.

Optional-to-optional dependencies are **avoided**. If one genuinely needs
another, declare it as a real dependency in `pyproject.toml` so pip installs
both — never rely on an import that happens to work because both are installed
today, and never on plugin initialization order. The graph stays acyclic and
`tests/architecture/test_packages.py` enforces it.

### The public SDK boundary

A package may import only what core intends to publish:

```python
from blitzecdn.core.plugins import (
    PluginMetadata,
    SiteStateContribution,
    ValidationIssue,
    hookimpl,
)
from blitzecdn.core.config import Settings          # configuration
from blitzecdn.core.operation_ports import PlaybookRunner  # ports it is handed
from blitzecdn.api.operations import OperationModel # to build a router
from blitzecdn.cli.common import ExitCode           # to build a command
from blitzecdn.features.sites import CdnSite        # a public capability contract
```

and never:

```
blitzecdn.bootstrap          # the control plane composes itself; a package composes itself
blitzecdn.api.app            # the application composition
blitzecdn.cli.main           # the command-line composition
blitzecdn.core.database*     # storage implementations, reached through ports
blitzecdn.core.ansible       # the concrete runner, reached through `platform.fleet`
*.persistence, *._private    # another distribution's internals
```

The allowlist is written out in `_PUBLIC_SDK_PREFIXES` in
`tests/architecture/test_packages.py`; adding to it is a deliberate decision
about what BlitzeCDN promises an installed capability. There is no separate
`blitzecdn-sdk` distribution, and there should not be one until something
concretely needs it.

### How it composes itself

`bootstrap.py` builds the control plane's *required* services and knows nothing
about what is installed beside it, so a package builds its own service in its
own `composition.py`, from what the control plane publishes:

```python
def build_cache_service(platform: ControlPlane) -> CacheService:
    return CacheService(
        sites=platform.sites,     # the read side of the site model, as a port
        events=platform.events,   # the domain-event recorder
        runner=CachePlaybooks(platform.settings, platform.fleet),
    )
```

`platform.fleet` is a `PlaybookRunner`: "run this named play with these
variables against these hosts", and nothing feature-shaped. Core stages the
variables file, expands the host limit and applies the timeout; what a purge
document looks like is the cache package's business, in its own adapter. That
is why `run_cache_purge` is no longer a method on core's `AnsibleRunner`.

### How it owns its tests

They live in `packages/<name>/tests/` and run with the rest of the workspace.
The package is the unit of modularity, so removing the directory removes the
implementation *and* its tests in one move. Core's suite keeps only core
behavior, the plugin contracts, the architecture rules, cross-package contract
tests and integration tests — and
`test_the_control_plane_suite_names_no_optional_package` fails if a core test
imports one, because core's tests must pass with nothing optional installed.

The control plane's shared fixtures are reachable from a package's tests through
`control_plane_fixtures`, registered as a pytest plugin by the workspace's root
`conftest.py`.

### Attaching and detaching

In the workspace:

```bash
uv sync --all-packages                  # everything, for development
uv sync                                 # the control plane alone
uv sync --extra compression --extra security  # core plus selected capabilities
uv sync --extra http3                   # core plus HTTP/3 over QUIC
uv sync --extra geoip                   # core plus visitor country lookup

uv add --package blitzecdn blitzecdn-cache      # attach
uv remove --package blitzecdn blitzecdn-cache   # detach
```

Beside an installed control plane:

```bash
pip install 'blitzecdn[compression]'  # attach one official capability
pip uninstall blitzecdn-compression   # detach it
pip install 'blitzecdn[all]'     # the control plane and every optional capability
pip install blitzecdn            # the control plane and none of them
```

`pip install blitzecdn` pulls in no optional distribution: they are extras, not
dependencies. `install.sh` and the container image select their required extras,
and `BLITZECDN_CAPABILITIES` overrides that list
for a controller that should ship without one. Keep `backup` installed on a
controller you intend to update in place — `install.sh update` takes a database
backup before it changes anything.

Testing:

```bash
just install                       # uv sync --frozen --all-packages
just test                          # the whole workspace, the gate
just test-package blitzecdn-cache  # one distribution's own tests
just test-core-only                # the suite with nothing optional installed
just build                         # every wheel and sdist
```

`just test-core-only` syncs the optional packages away, runs the control plane's
suite, and syncs them back. It is part of `just check`.

### Installed, enabled, configured

Three different things, and collapsing any two of them is a bug:

```
blitzecdn-compression installed → the capability is available on this controller
site.compression = off          → it is available but this site does not request it
site.compression = brotli       → this site requests the available capability
```

Removing a package is **not** the same as configuring the capability off.
The site contract remains readable after detachment. A requested implementation
then fails deployment validation with `capability '<token>' is not installed,
and this site's <setting> requests it`; it is not ignored and its policy is not
rewritten. The token comes from the site's own `capability_requirements` and the
answer from plugin metadata — one generic mechanism for `compression`, `http3`,
`geoip` and a capability this repository has never heard of alike.

The official site-level absence rules are:

| package | valid without it | requires it |
| --- | --- | --- |
| `blitzecdn-compression` | `compression = off` | `gzip` or `brotli` |
| `blitzecdn-certificates` | TLS disabled, or `certificate_mode = existing` with `ssl_automatic_mode = custom` | uploaded/requested material, or Automatic SSL on active TLS |
| `blitzecdn-security` | Under Attack off and an empty site firewall | Under Attack Mode or any site firewall rule |
| `blitzecdn-http3` | `http3_enabled = false` — the site is still served over HTTP/1.1 and HTTP/2 | `http3_enabled = true` |
| `blitzecdn-geoip` | no country header and no country rules — every other firewall rule is unaffected | `visitor_headers.ip_country`, `firewall.allowed_countries` or `firewall.denied_countries` |

TLS policy itself always stays core. Certificate routes, `cert`/`ssl` commands,
certificate jobs, issuance, renewal, upload, and Automatic SSL scans appear only
while `blitzecdn-certificates` is installed.

### Baseline HTTP versus optional HTTP/3

HTTP/1.1 and HTTP/2 are invariants of a managed edge. They are served by every
edge, they have no policy to set, and no distribution has to be installed for
them to work — the built-in `http` capability owns them and is `required`.

HTTP/3 is the advanced transport a site opts into, and its implementation is
`blitzecdn-http3`:

| `blitzecdn-http3` | `http3_enabled` | What happens |
| --- | --- | --- |
| absent | `false` | The site is served over HTTP/1.1 and HTTP/2. Nothing is missing, nothing is reported. This is an ordinary installation. |
| absent | `true` | The site still **loads** — the field is core's. The deployment is refused at validation with `capability 'http3' is not installed`, before any playbook runs. It is never silently downgraded to HTTP/2 and the setting is never ignored. |
| installed | `true` | The fleet opens a QUIC listener and exactly one site is named to carry `reuseport`. |
| installed | `false` everywhere | Identical to the detached fleet document. Installing the package converges nothing on its own. |

Attach it with `uv sync --extra http3` in a checkout, `pip install
'blitzecdn[http3]'` beside an installed control plane, or by adding `http3` to
`BLITZECDN_CAPABILITIES` for `install.sh` and the container image.

Two fleet variables describe the listener, and both are `required: true` in the
`blitzecdn_edge` and `blitzecdn_nginx` argument specs:

```
blitzecdn_edge_http3_enabled        # does any enabled site serve HTTP/3
blitzecdn_nginx_http3_listener_owner # the one server block carrying reuseport
```

They are always present, whatever is installed, so Ansible sees one contract
rather than a document whose shape depends on the controller's wheels. The
built-in `http` plugin writes them at their baseline — no listener, no owner —
and `blitzecdn-http3` declares both in its `overrides` and replaces them with
the values it derives from the fleet. That is the same mechanism
`blitzecdn-certificates` uses for the certificate paths `sites` projects.

Ansible remains the provisioning authority for how the edge *realizes* this.
The Nginx HTTP/3 directives, the QUIC and UDP listener rendering, the firewall's
UDP/443 rule, the Nginx module capability probe, the edge image build and the
integration harness all stay in the roles. The package owns whether the
capability is offered at all, the two fleet variables, the validation that
refuses a site without it, and its own tests.

### Visitor country lookup

`blitzecdn-geoip` is optional. Sites that do not request country-aware behavior
do not need it, and that is most sites: serving a hostname, filtering by source
address, method or path, compression, TLS and every HTTP version work with
nothing installed beside the control plane. Enabling a country header or
country-based policy requires the `geoip` capability.

Exactly three stable settings ask the edge which country a visitor is in:

| setting | owner | what it asks for |
| --- | --- | --- |
| `visitor_headers.ip_country` | site header policy | the `BZ-IPCountry` request header written to the origin |
| `firewall.allowed_countries` | `SecurityPolicy` | serve only these countries; everything else gets a 403 |
| `firewall.denied_countries` | `SecurityPolicy` | answer these countries with a 403 |

Two owners, one capability. They resolve the same question against the same
GeoIP2 database and the same Nginx module, so there is one distribution rather
than one per consumer — and a fourth consumer (analytics, geographical routing)
attaches to the same token rather than adding a wheel.

| `blitzecdn-geoip` | the site asks for a country | What happens |
| --- | --- | --- |
| absent | no | Served normally. Nothing is missing, nothing is reported. This is an ordinary installation. |
| absent | yes | The site still **loads** — the fields are core's. The deployment is refused at validation naming the `geoip` capability *and* the setting that requested it, before any playbook runs. No country header is silently omitted and no country rule is silently dropped. |
| installed | yes | The site deploys, and the edge resolves the visitor address against its GeoIP2 database. |
| installed | no | Identical to the detached fleet document. Installing the package converges nothing on its own. |

A firewall country list requires **two** capabilities: `security` for the rule
and `geoip` for the lookup. They are reported separately, because detaching
either one is a different problem. `blitzecdn-security` does not import
`blitzecdn_geoip` and never will — it depends on the *token*, which is what
lets a different distribution answer for `geoip` one day.

Attach it with `uv sync --extra geoip` in a checkout, `pip install
'blitzecdn[geoip]'` beside an installed control plane, or by adding `geoip` to
`BLITZECDN_CAPABILITIES` for `install.sh` and the container image. It is **not**
in the default installation: country lookup is opt-in, and adding it by default
would make an ordinary site depend on a MaxMind account.

This package contributes no desired state, and that is deliberate. Whether an
edge resolves countries at all is fleet Ansible policy —
`blitzecdn_edge_geoip_enabled`, set with `blitzecdn config set` or in group vars
— not a variable the control plane derives per deployment, so turning it into a
contribution would silently override what an operator configured. The rendered
document is byte-identical with and without the package installed.

Ansible remains the provisioning authority for how the edge realizes the
lookup. The MaxMind credentials, the `geoipupdate` image, the GeoLite2-Country
database and the systemd timer that refreshes it, the mount into the Nginx
container, the `geoip2` directive and its `auto_reload`, and the
`ngx_http_geoip2` module probe all stay in `blitzecdn_edge`,
`blitzecdn_edge_stack` and `blitzecdn_nginx`. The database refresh is a
provisioning lifecycle rather than recurring control-plane behavior, so the
package contributes no scheduled job and performs no network download. The
role's own assertion — refusing a country-dependent site on an edge with GeoIP
off — stays too: it is the second line, for a desired state that did not come
from this control plane.

### Seeing what is installed

```bash
blitzecdn plugins          # every capability, and anything that failed to load
blitzecdn plugins --json
```

The answer to "I installed `blitzecdn-cache` and there is no `cache purge`". A
*required* capability that failed would have stopped the process; an optional
one is reported and skipped by design, and a warning that scrolled past at
startup is not somewhere an operator can look afterwards — so the reason is kept
on `registry.rejected` and printed here.

`required` separates a capability this distribution ships from one installed
beside it, and `capabilities` are the tokens `required_capabilities` matches
against.

### When a package is absent

Absence on its own is silent, because detaching is a supported operation and a
control plane that warned about every capability nobody installed would warn
about every capability that does not exist.

An installation that has *decided* it depends on one says so, and then absence
is fatal:

```toml
# blitzecdn.toml
[blitzecdn]
required_capabilities = ["cache", "backup"]
```

```bash
# or, taking precedence over the file
BLITZE_REQUIRED_CAPABILITIES=cache,backup
```

It is a `Settings` value, so it follows the normal configuration precedence —
environment over `blitzecdn.toml`. It is deliberately *not* a `blitzecdn config
set` key: that command manages fleet-wide Ansible policy published to the edges,
and which capabilities this controller must have is a fact about the controller.

Startup then refuses, naming the missing token and listing what is installed:

```
this installation's `required_capabilities` requires the capability cache,
which no installed plugin provides. Installed capabilities: backup,
compression, deployments, ... Install the distribution that supplies it, or
remove it from `required_capabilities`.
```

The mechanism is generic. The tokens come from configuration, the answer comes
from `PluginMetadata.provides`, and nothing between them names a feature — there
is no `if feature == "cache":` anywhere, and a capability this repository has
never heard of is checked identically.

### Persistence and detachment

Detaching is **non-destructive**. The official optional packages own no schema
migration. Core retains site policy and certificate metadata needed to read
existing desired state; re-attaching restores the operational contribution.

An optional capability that genuinely needed its own persistence would have to
define an install/upgrade/uninstall policy first — and uninstalling must never
destroy its data. Do not build a general migration framework for a hypothetical
one.

### Ansible stays where it is

Ansible remains the provisioning authority. Playbooks, Nginx templates,
certificate-file deployment, module/runtime probes, firewall application, and
edge roles remain in the control plane's Ansible tree. Detaching a Python
package removes availability, validation, or operational contributions, never
the role that realizes stable desired state. There is no runtime downloading of
roles from plugin packages.

## Adding an optional capability

1. Decide it really is one. Answer the two questions in [What is *not* a
   package](#what-is-not-a-package). If it implements a `CdnSite` field, keep
   that stable field/enum in core and extract only the implementation.
2. Create `packages/blitzecdn-<name>/` with a `pyproject.toml` declaring
   `dependencies = ["blitzecdn>=3.0.0,<4"]`, the `blitzecdn.plugins` entry
   point, and `[tool.uv.sources] blitzecdn = { workspace = true }`.
3. Write `src/blitzecdn_<name>/plugin.py` with
   `blitzecdn_plugin_metadata` (`required=False`, `provides={...}`) and the
   hooks it contributes through, and `composition.py` if it needs a service.
4. Put its tests in `packages/blitzecdn-<name>/tests/`.
5. Add the extra to `[project.optional-dependencies]` in the root
   `pyproject.toml`, the workspace source to `[tool.uv.sources]`, and the test
   path to `testpaths`.
6. `uv lock && uv sync --all-packages`.

Nothing in `src/blitzecdn/` changes. There is no step that registers it.

## Adding an external plugin

A distribution from outside this repository — `blitzecdn-waf`,
`blitzecdn-ratelimit`, `blitzecdn-monitoring` — is the same thing without step 5:
it declares itself in the entry-point group

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
entry-point machinery by `tests/architecture/test_plugins.py`.

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

* An **optional package** may depend only on the public SDK above, on public
  capability contracts, and on its own modules — never on another optional
  package's internals, and never on `bootstrap`, the entry-layer compositions,
  or the storage implementations.
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

Every one of these is a test in `tests/architecture/test_layering.py` (the
control plane's internal boundaries) or `tests/architecture/test_packages.py`
(the distribution boundary), not a convention. The packaging lifecycle itself —
build, install, discover, uninstall, and the deterministic failure when a
required capability is absent — is `tests/architecture/test_lifecycle.py`,
marked `packaging` because it builds real wheels.
