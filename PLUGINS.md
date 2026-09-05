# Plugins

BlitzeCDN is a capability-oriented modular monolith whose capabilities
register themselves. `pluggy` is the mechanism. This page is the contract: what it is
used for, what it must never be used for, and how to add a capability — as a
required part of the control plane, or as a distribution installed beside it.

## Three categories, and the difference between them

Everything in this repository is exactly one of these, and the distinction is
load-bearing rather than descriptive:

| | where it lives | how it registers | can it be absent? |
| --- | --- | --- | --- |
| **1. Core** | `src/blitzecdn/core/` | it *is* the control plane | no |
| **2. Built-in required capabilities** | `src/blitzecdn/capabilities/` | `composition.BUILTIN_PLUGINS` | no — a failure is fatal |
| **3. Installable optional capabilities** | `packages/blitzecdn-*/` | the `blitzecdn.plugins` entry-point group | yes, and that is normal |

The third category is a real Python distribution. The official optional wheels
are `blitzecdn-backup`, `blitzecdn-cache`, `blitzecdn-compression`,
`blitzecdn-certificates`, `blitzecdn-security`, `blitzecdn-http3`,
`blitzecdn-geoip`, `blitzecdn-hardening`, `blitzecdn-origins` and
`blitzecdn-resolver`. They install
beside the
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

### The classification, in full

Every significant capability, and where it landed. The right-hand column is the
answer to the question above, not a description of the code.

| Capability | Classification | Why |
| --- | --- | --- |
| plugin infrastructure, persistence, runtime, config, Ansible execution | **core** | the control plane *is* these; there is nothing left without them |
| sites | **built-in required** | `CdnSite` is what every other capability composes into and every edge role renders from |
| DNS, edges, deployments, diagnostics, maintenance | **built-in required** | a CDN with no zones, no fleet, no way to converge it and no way to see whether it worked is not a degraded CDN |
| HTTP/1.1 and HTTP/2 | **built-in required** | baseline. An edge that speaks neither serves nothing, and there is no `blitzecdn-http1` to attach |
| compression, security, TLS *contracts* | **built-in required** | `CompressionPolicy`, `SecurityPolicy` and `TlsPolicy` are inherited into the flat `CdnSite` that the published schemas, the persisted policy JSON and the deployment snapshots consume; a field that travelled with a wheel would make a stored row unreadable on detachment |
| `blitzecdn-backup` | **optional** | archiving and restoring the control plane's own state is an operational choice, and the capability has no Ansible at all |
| `blitzecdn-cache` | **optional** | purge and cache-effectiveness *operations*, with their own roles and plays. `CachePolicy` stays in `sites/policy/` — nothing outside a site's own configuration reads it, and moving it here would make `sites` depend on a feature that already depends on `sites` |
| `blitzecdn-certificates` | **optional** | issuance, renewal and the Automatic SSL/TLS scan. An operator may bring their own certificates and never attach it |
| `blitzecdn-compression` | **optional** | gzip and Brotli are `CompressionMode` values inside one capability, not two wheels. The per-vhost directives stay in `site.conf.j2`: they are site settings, and an absent capability is refused by name before a play starts |
| `blitzecdn-geoip` | **optional** | one lookup, two consumers — the `BZ-IPCountry` header and the country firewall lists — so one wheel, and it brings its whole edge implementation |
| `blitzecdn-hardening` | **optional** | the edge *host's* front door: public-key-only SSH and a Fail2Ban jail, and the role that takes both off a decommissioned host. No site setting asks for it, so its absence is silent by design — detaching is how a fleet says something else owns `sshd_config` |
| `blitzecdn-http3` | **optional** | the QUIC listener and its fleet derivation. The *switch* stays a field on `ProtocolPolicy`; what detaches is the implementation behind it |
| `blitzecdn-origins` | **optional** | asking the edges whether they can reach the origins they proxy to. An *operation*, like a purge: no lock, no desired state, nothing converged. `OriginCheck` — the single-origin row — stays in core, because certificate preflight's controller-side probe produces one without running any play |
| `blitzecdn-resolver` | **optional** | what an edge *host* resolves names with. Like `hardening`, no site setting asks for it and detaching is how a fleet says its DNS belongs to the network. Its drop-in lives at a path core must not name, so it fills the decommission slot too |
| `blitzecdn-security` | **optional** | the njs challenge and the per-site rule validation. The rule *vocabulary* stays in `SecurityPolicy` for the same reason as the other site contracts |

Two that came up and were deliberately **not** split:

* **WAF and rate limiting.** Neither exists yet. When they do, the question is
  cohesion and lifecycle, not package count: a WAF that shares Under Attack
  Mode's njs runtime, its fleet secret and its per-site rule shape belongs
  inside `blitzecdn-security`; one with its own rule format, its own signature
  feed and its own update schedule is a distribution of its own. Rate limiting
  reads the same `$binary_remote_addr` zone machinery the challenge already
  installs, which argues for `blitzecdn-security` unless it grows a
  distributed counter store.
* **`blitzecdn_nginx`.** It is not one capability's implementation. It renders
  whatever the merged desired-state document holds, so it stays core; see
  [What stays in `blitzecdn_nginx`, and
  why](#what-stays-in-blitzecdn_nginx-and-why).

### The canonical package

Every optional distribution converges on one shape. Small ones use fewer
files — `blitzecdn-compression` is a `plugin.py` and nothing else, and that is
correct rather than incomplete — but nothing uses a *different* shape.

```
packages/blitzecdn-<name>/
├── pyproject.toml            # the entry point, and a dependency on blitzecdn only
├── README.md                 # what it owns, what it does not, and its BLITZE_* names
├── LICENSE
├── src/blitzecdn_<name>/
│   ├── plugin.py             # metadata + the hooks it contributes through
│   ├── composition.py        # builds its service from what the platform publishes
│   ├── config.py             # its own settings, read from capability_environment
│   ├── domain/               # pure rules — named for the values they hold
│   ├── ports.py              # the narrow Protocols *it* calls
│   ├── service/              # the capability's behaviour — named for what it
│   │                         #   decides: `issuance.py`, `convergence.py`
│   ├── adapters/             # concrete implementations of its own ports
│   │                         #   named after what they implement, and flat
│   ├── policy.py             # its configuration contract, if it has one
│   ├── api/                  # its HTTP adapters — flat, named for what they
│   │   ├── models.py         #   are: its own operational shapes,
│   │   └── routes.py         #   the routes it contributes, a second router
│   │                         #   where the auth posture differs
│   ├── cli.py                # its command groups — a directory once one
│   │                         #   group edits several capabilities' settings
│   ├── nginx/                # *.conf.j2 fragments only
│   └── ansible/
│       ├── __init__.py       # ROLES_PATH and EDGE_ROLE, via package_directory
│       ├── roles/<role>/     # defaults, tasks, handlers, templates, argument_specs
│       └── playbooks/        # plays only this capability runs
└── tests/                    # including its role's contract tests
```

A package with no service needs no `composition.py`; one with no settings needs
no `config.py`; one that converges no edge ships no `ansible/`. Do not add a
`domain/`, `application/` and `infrastructure/` split to a package of six
modules — the layering is already expressed by the names above, and the
architecture tests check it there.

**Four of those names are layers, and a layer is always a directory.**
`domain/`, `service/`, `adapters/` and `api/` are directories in every
capability that has them, holding one module or five: `domain/` with `site.py`
beside `patch.py`, `service/` with `convergence.py` beside `rollback.py`,
`domain/` with `zones.py` alone. The contents are named after *what they are* —
`checks.py`, `playbooks.py`, `preflight.py`, `issuance.py` — never after the
layer again, and the directory is flat. `policy` and `cli` are the two that may
still be either, because a contract is often one class and a command group is
often one screen of commands; `sites` spells both as directories and the other
capabilities do not.

This was "a file until it outgrows one", and the tree that produced had the
same layer as a file in five capabilities and a directory in two — so a reader
learned the shape from whichever slice they opened first, and a contributor had
to decide per slice which spelling this one used. Fifteen layers became
directories holding a single module to end that, and `__init__` re-exports what
the file used to, so `dns.service` still means what it always did. What it buys
is that every slice answers "where does behaviour live" the same way, and that
adding a second module to a layer is putting a file in a directory rather than
converting one first.

That is not a style preference. `tests/architecture/test_layering.py` decides
which rule a module lives under by asking where it sits: everything in `domain`
is refused framework and I/O imports, everything in `service` is refused a
concrete adapter, everything in `adapters` is refused the entry layers and the
composition root. Putting a file in the right directory is how it gets the
right rule, and a new module is inside the rule the day it is written rather
than when somebody remembers to add its name to a list — which is exactly what
those lists used to be, and what they missed.

`blitzecdn-cache` is the reference for a package with a service, an API, a CLI
and plays of its own; `blitzecdn-geoip` is the reference for a package whose
whole substance is its edge role.

**A built-in capability keeps the same shape, including `api/models.py`.** The
HTTP representation of a site, a record, an edge or a deployment lives beside
the routes that publish it, exactly as a package's `PurgeResult` does. What
stays in `blitzecdn.api.models` is the frame every representation is built from
— the base model and the `as_operation` projection — and the shapes of what
*core* holds: an Ansible run, a workflow, an audit entry. A shared module of
resource models was the one place every capability had to edit and none owned.

**That list is a vocabulary, and it is enforced.** Ten packages that converged
on a shape by imitation give the eleventh author ten examples and no rule,
which is how one package's settings ended up in `config.py` and another's
inline in `plugin.py`. `test_a_package_organises_its_python_into_the_documented_modules`
in `tests/architecture/test_packages.py` walks each distribution's `src/` and
refuses a module name that is not one of the above, and
`test_a_built_in_capability_organises_its_python_the_same_way` walks
`src/blitzecdn/capabilities/` and holds the built-ins to the same set — one
vocabulary, whichever side of the packaging boundary a capability is on.
`adapters/`, `api/`, `cli/`, `domain/`, `policy/` and `service/` are flat, and
their module names are the things they hold rather than the layer again —
`domain/domain.py` is refused by name; `nginx/` and `ansible/` carry no Python
beyond the one `core.runtime.resources.package_directory` anchor.

That the set is *closed* is what lets the layering rules be positional rather
than hopeful. `_entry_files` in `test_layering.py` identifies a capability's
delivery adapters as "everything under `api/` and `cli`" — it was a literal
list of file names, so `readiness.py` was covered because somebody remembered
to add it and a slice growing a `commands.py` would have been covered by
nothing. Both are directories or files by the same rule now, so splitting
`sites/cli.py` into `sites/cli/` cannot take its commands out from under the
entry rule. `api/` and `cli` stay outside `adapters/`, which would make both
positional, because they are not private the way an adapter is: `api/models.py`
is a published contract, and `adapters/` is a directory the entry layers are
forbidden to reach.

Growing the vocabulary is allowed and is a *decision*: add the name to the set
in the test with the sentence saying what belongs in it, and to the tree above.
`test_the_documented_layout_and_the_enforced_one_are_the_same` fails when only
one of the two registers moves. A module outside the vocabulary altogether is
declared per-distribution with its reason — `blitzecdn_certificates.acme_hook`
is the only one, because certbot execs it as a subprocess and it is a second
composition root rather than part of the capability.

A capability may nest **one** level and no more.
`blitzecdn-certificates` holds `certificates/` and `automatic_ssl/`, and each
follows the same vocabulary, because issuing material and deciding when to
upgrade a mode are two jobs on one capability. A second level is a package that
wants to be two distributions; the test says so there rather than three
refactors later.

### Why site-policy contracts stay in core

`CompressionPolicy`, `SecurityPolicy`, and `TlsPolicy` cannot be extracted, and
the reason is worth writing down because it will come up again. `CdnSite`
composes `CompressionPolicy`, `ProtocolPolicy`, `SecurityPolicy` and `TlsPolicy`
**by inheritance** into one flat, frozen model, and that model is consumed by
the published HTTP schemas, by persisted policy JSON, by the versioned
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
`core.domain.validation.OmittedWhenEmpty`, and core prunes whatever declares it
without asking what it holds — deliberately *not* every nested block, because
`visitor_headers` carries switches whose default the role reads, so "never
configured" and "switched off" are not the same document.

## Core versus capabilities

**Core** (`src/blitzecdn/core/`) is what a capability is allowed to build on,
and it is the one package in this repository arranged by *layer* rather than by
slice — because it is the layer everything else stands on:

| | what is in it | who may import it |
| --- | --- | --- |
| `core/domain/` | values and vocabulary: runs, events, identifiers, validation, the contract base | anyone |
| `core/ports/` | the protocols a service declares instead of naming an implementation | anyone |
| `core/persistence/` | the engine, the physical schema, the stores | composition, and `persistence.schema` alone is published |
| `core/runtime/` | subprocesses, files, logging, the queue, where a wheel landed | composition and adapters |
| `core/config/`, `core/ansible/`, `core/plugins/` | settings, playbook execution, the plugin machinery | as documented below |
| `core/exceptions.py` | the one module every layer may name | anyone |

`core/application/` was a seventh row — "the workflow coordinator, and nothing
that orchestrates a capability" — and its own docstring called itself the
little application logic core is allowed to have. A coordinator with a domain
model, a port, a store and a table under it is a slice, and it is
`capabilities/workflows/` now. The package is gone rather than empty: an
application layer in a package arranged by layer is where the next
cross-capability service would have been filed without anybody deciding to.

Core owns no business capability. `tests/architecture/test_layering.py` fails a
`core` module that imports a capability's `service` or `adapters`, and fails a
capability's `domain` or `service` that imports `core.persistence` or
`core.runtime` — as packages, so a module added to either is inside the rule
without anybody editing a list.

The first three rows are also held apart from the rest.
`test_core_domain_and_ports_are_framework_and_io_independent` refuses
`core/domain/`, `core/ports/` and `core/exceptions.py` anything but the standard
library, pydantic and each other: no framework, no I/O, and no other part of
`blitzecdn`. That is what "anyone may import it" rests on, and it is why those
two packages are published *whole* to an installed capability while
`core/runtime/` and `core/persistence/` publish one named module at a time. The
rule is positional, like the ones above it — a module is inside it by sitting in
one of those directories, so an I/O module written into `core/domain/` is
refused on the day it appears rather than when a reviewer notices it has joined
the public SDK.

**Capabilities** (`src/blitzecdn/capabilities/`) contain required capabilities
and stable site contracts: `sites`, `dns`, `http`, `tls`, `cache`,
`compression`, `security`, `deployments`, `edges`, `diagnostics`,
`maintenance`, `workflows`. A contract-only directory — `cache`, `compression`, `security`,
and `http` and `tls` beyond their fleet-level plugin — is one whose
*implementation* ships as a wheel: what stays here is the shape of the setting,
so a stored site still reads back on a controller that has neither. Small
capabilities need not invent service, repository, or adapter layers merely to
match a directory template.

**Optional capabilities** (`packages/`) are the same shape one directory out,
and are covered in [Installable optional capabilities](#installable-optional-capabilities).

One top-level package is one capability. Certificate issuance and the Automatic
SSL/TLS scan are two parts of `blitzecdn-certificates`, not separate packages.
`tests/architecture/test_layering.py` refuses a top-level `capabilities/gzip`,
`capabilities/http3`, `capabilities/certificates` or
`capabilities/under_attack` package by name — a *feature directory* named after a strategy, a version or a mode is
always wrong. An optional distribution is a different question: it is named
after the implementation an operator attaches or removes, which is why
`blitzecdn-certificates` and `blitzecdn-http3` exist as wheels while
`capabilities/certificates` and `capabilities/http3` remain refused.

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

**`blitzecdn.composition`** is the composition root, and the only place
production wiring lives. `control_plane.py` builds adapters, injects them into
services through constructors, loads the plugins, and hands each plugin the
finished control plane so it can register what it contributes; `repository.py`
chooses which capability stores sit on one SQLite file, and `scheduler.py`
which of the contributed jobs get triggers.

It is a package rather than three loose modules at `src/blitzecdn/`'s root
because the root answers a different question: which processes exist. `api/`,
`cli/`, `worker.py` and `install_handoff.py` are the four, and
`test_a_loose_module_at_the_root_starts_a_process` holds the line by checking
that something outside Python — the installer, a compose file — names each
one.

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

Eleven, all prefixed `blitzecdn_`. A hook whose contribution is static takes no
arguments; every other hook takes `platform`, the built control plane.

| hook | arguments | returns |
| --- | --- | --- |
| `blitzecdn_plugin_metadata` | – | `PluginMetadata` (required of every plugin) |
| `blitzecdn_api_routers` | – | `Sequence[APIRouter]` |
| `blitzecdn_cli_commands` | – | `Sequence[CliCommandGroup]` |
| `blitzecdn_ansible_contributions` | – | `Sequence[AnsibleContribution]` |
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

So `composition.BUILTIN_PLUGINS` can be reordered freely and no edge converges
differently.
Never concatenate configuration text through a hook — contribute typed values
and let the edge roles render them.

## Adding a built-in required capability

First answer: should this be optional instead? A capability BlitzeCDN can
operate coherently without belongs in `packages/`, not here. Then:

1. Create `src/blitzecdn/capabilities/<name>/` with only the domain, service,
   ports, and entry adapters its existing responsibilities require, using the
   same vocabulary a package uses — `domain`, `policy`, `ports`, `service`,
   `adapters/`, `api/`, `cli`, `plugin.py`, `composition.py`. Keep it small:
   one module inside each layer directory until there are two things in it.
2. Write `plugin.py` with `blitzecdn_plugin_metadata` (`required=True`) and the
   hooks it contributes through.
3. Add the module path to `BUILTIN_PLUGINS` in `composition/control_plane.py`.
   The roster is
   the composition root's, not `core`'s: `core.plugins` registers the module
   paths it is handed and names no capability at all, which
   `test_core_names_no_capability_even_in_a_string` holds.
4. Build the service in the same file with explicit constructor injection.
5. Add the capability to `ALLOWED_CAPABILITY_DEPENDENCIES` in
   `tests/architecture/test_layering.py`, declaring every other capability it
   depends on — and to `ALLOWED_POLICY_DEPENDENCIES` if it has a `policy.py`
   contract or composes another capability's.

Before step 1, answer the question the structure exists to make obvious:
**which existing capability owns this?** A new top-level feature is the
exception. An option, a strategy, an encoding, a protocol version or a mode
belongs inside the capability that already owns its behavior.

Built-ins are an explicit tuple rather than entry points on purpose: the control
plane *is* these capabilities, so resolving them through installation metadata would
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
│   ├── domain/  service/  adapters/  api/   # layers, always directories
│   ├── policy.py  ports.py  cli.py          # a file, or a directory if it grows
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

`__version__` is `distribution_version(__name__)`, never a literal: it is
what `blitzecdn plugins` shows an operator, so it is read back from the
installed metadata rather than copied out of `pyproject.toml` and left behind
by a release.

```python
__version__ = distribution_version(__name__)


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
from blitzecdn.core.config import Settings  # configuration
from blitzecdn.core.ports.operations import PlaybookRunner  # ports it is handed
from blitzecdn.core.runtime.resources import (  # where its files landed, and its version
    distribution_version,
    package_directory,
)
from blitzecdn.api.models import Model, as_operation  # to build a router
from blitzecdn.cli.common import ExitCode  # to build a command
from blitzecdn.capabilities.sites import CdnSite  # a public capability contract
```

and never:

```
blitzecdn.composition        # the control plane composes itself; a package composes itself
blitzecdn.api.app            # the application composition
blitzecdn.cli.main           # the command-line composition
blitzecdn.core.persistence   # storage implementations, reached through ports
                             #   except `persistence.schema`, which a backup reads
blitzecdn.core.ansible       # the concrete runner, reached through `platform.fleet`
*.persistence, *._private    # another distribution's internals
```

The allowlist is written out in `_PUBLIC_SDK_PREFIXES` in
`tests/architecture/test_packages.py`; adding to it is a deliberate decision
about what BlitzeCDN promises an installed capability. There is no separate
`blitzecdn-sdk` distribution, and there should not be one until something
concretely needs it.

### How it composes itself

`blitzecdn.composition` builds the control plane's *required* services and
knows nothing about what is installed beside it, so a package builds its own
service in its own `composition.py`, from what the control plane publishes:

```python
def build_cache_service(platform: ControlPlane) -> CacheService:
    return CacheService(
        sites=platform.sites,  # `sites.ports.SiteReader`: list and get, no writes
        events=platform.events,  # the domain-event recorder
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

A built-in capability owns its tests the same way, one directory further out:
they live in `tests/capabilities/<name>/`, and
`test_a_built_in_capabilitys_tests_live_in_its_own_directory` fails a
capability that decides something — one with a `service/` or an `api/` — and
has nowhere of its own to assert it. A contract capability is exempt: `cache`,
`compression` and `tls` are pydantic models composed into `SitePolicy`, and
they are tested where they compose, in `tests/capabilities/sites/` and
`tests/contract/`.

What the rule refuses is a capability's own decisions asserted somewhere that
is about something else. `MaintenanceService` was tested in the Dramatiq suite,
which the service does not touch; `WorkflowCoordinator` and `check_resolver`
had no direct test at all. Cross-cutting suites stay where they are —
`tests/platform/`, `tests/api/` and `tests/entrypoints/` test the assembled
thing, and importing a capability's domain types to do it is not a misplaced
capability test.

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

`blitzecdn-hardening` and `blitzecdn-resolver` are deliberately absent from
that table: no site setting asks for either, so no site is ever refused for
their absence. They are the optional capabilities whose presence changes
nothing about desired state — only what core's plays do to the host, after the
edge is serving in one case and before the renderer runs in the other.

TLS policy itself always stays core. Certificate routes, `cert`/`ssl` commands,
certificate jobs, issuance, renewal, upload, and Automatic SSL scans appear only
while `blitzecdn-certificates` is installed.

### What a capability may ask an operator to configure

A capability's configuration is not a field on `Settings`. It is *claimed*,
through one hook, in one contract:

```python
@hookimpl
def blitzecdn_capability_configuration() -> Sequence[ConfigurationContribution]:
    return (
        ConfigurationContribution(
            plugin="blitzecdn-certificates",
            environment_keys=(
                EnvironmentKey(
                    name="BLITZE_ACME_EAB_HMAC_KEY",
                    summary="external account binding key, from the CA's dashboard",
                    minimum_bytes=32,
                ),
            ),
            settings=(
                CapabilitySetting(
                    name="BLITZE_CERTBOT_BINARY",
                    default="certbot",
                    summary="the certbot executable this controller runs",
                ),
                CapabilitySetting(
                    name="BLITZE_RENEWAL_INTERVAL_HOURS",
                    default=12,
                    minimum=0,
                    summary="how often renewal runs; 0 disables the job",
                ),
            ),
        ),
    )
```

**Two kinds, and the difference is what core is allowed to do with the value.**
An `EnvironmentKey` is a secret: it stays a `SecretStr` end to end, it is the
only kind copied into Ansible's subprocess environment, it never travels
through argv or desired state, and nothing prints it — `blitzecdn plugins`
reports whether it is `set`, not what it is. A `CapabilitySetting` is a
non-secret: core resolves it to the type its `default` carries, checks the
bounds, and hands it back, and the CLI shows the resolved value.

The default carries the type, which is why there is no `kind` field beside it:
`default=12` is an `int` and `default="12"` is a `str`, and two ways to say one
thing is two ways for them to disagree. Four types exist — `str`, `int`, `bool`
and `Path` — because a count, a flag, a name and a location are what settings
actually are. A relative `Path` default resolves against the controller's state
directory. `minimum` and `maximum` apply to whole numbers only, and are the
bounds core can enforce without knowing what a value *means*; anything richer
stays the capability's to validate, because a core that could describe a MaxMind
account id would be carrying the shape of a wheel that may not be installed.

**Where a value may come from, and which wins.** The environment and the
controller's `.env` — 0600 and uncommitted — carry both kinds. `blitzecdn.toml`
is neither, so it carries settings only, and a secret written there is refused
by name rather than quietly read. The environment wins over the file. In the
file the same name is written lowercase and without the `BLITZE_` prefix.

**Claiming is the whole mechanism.** Core stages every non-core `BLITZE_*` name
it can see, from all three sources, and refuses any that no installed
capability claims. That single rule is what makes a typo, a setting left behind
by a package that was detached, and a package that was never installed all
loud: without it, the three are indistinguishable from a value that is simply
being ignored, which is how an operator spends an afternoon on a credential
that was reaching nothing. Two capabilities claiming one name is refused for
the same reason — the namespace is shared, and either could reconfigure the
value out from under the other.

What each capability then gets back is *scoped*: `CapabilityConfig.for_plugin`
returns the keys that plugin declared and nothing else, so a package cannot
read another's credential, and asking for a name it did not declare is an error
rather than an empty string.

`portable=False` marks a setting that must not survive a restore onto a
different host — `blitzecdn-backup`'s own archive directory is the standing
case, because restoring it would point a rebuilt controller's backups at a path
belonging to the machine that died. Everything else is portable: an interval,
an executable name and a CA identity are decisions an operator made, and losing
them on recovery is losing configuration. Core cannot tell which is which for a
capability it has never heard of, so the capability declares it.

A capability that needs no configuration does not implement the hook.

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
`blitzecdn_geoip_enabled`, set with `blitzecdn config set` — not a variable the
control plane derives per deployment, so turning it into a contribution would
silently override what an operator configured. The rendered document is
byte-identical with and without the package installed.

What the package *does* contribute is its edge implementation, in full. The
`blitzecdn_geoip` role ships inside the wheel and carries the MaxMind
credentials, the `geoipupdate` image and its own small Compose project, the
GeoLite2-Country database, the systemd timer that refreshes it, the `geoip2`
directive with its `auto_reload`, and the assertion that refuses a
country-dependent site on an edge where the capability is switched off. Core's
edge play runs it because the contribution says so, and detaching the
distribution takes every one of those off the next deploy.

Two things stay core's, and both are seams rather than implementation. The
runtime contract's `paths.data` is a directory core creates and mounts
read-only into the edge container and the configuration-test container; the
role puts its database under it, and core never learns what is in there. The
generic `$blitzecdn_visitor_ip_country`/`BZ-IPCountry` contract is stable;
package-owned HTTP and upstream resources implement the GeoIP2 lookup and
assign that generic variable.

The database refresh is a provisioning lifecycle rather than recurring
control-plane behavior, so the package contributes no scheduled job and the
control plane performs no network download.

Its credentials are `BLITZE_MAXMIND_ACCOUNT_ID` and
`BLITZE_MAXMIND_LICENSE_KEY` in the controller's `.env`. Core no longer has
fields for either. The GeoIP Ansible contribution claims both keys explicitly,
and only resolved plugin claims enter the Ansible environment.

### The edge challenge

`blitzecdn-security` is the same shape. Core owns the `under_attack_mode`
switch and firewall rule contract on `CdnSite`; the package owns its contributed
challenge/filtering resources and the njs module that
implements the challenge, the fleet secret it signs clearances with
(`BLITZE_UNDER_ATTACK_SECRET`), the `conf.d` snippet that imports the module,
and both halves of the refusal — a deployment check at `blitzecdn validate`
that names the missing secret, and the role's own assertion for a desired state
that did not come from this control plane.

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
against. `configuration` lists what the capability claimed, each entry marked
`kind: secret` or `kind: setting` — a secret reporting only whether it is
`set`, a setting its resolved value. See
[What a capability may ask an operator to configure](#what-a-capability-may-ask-an-operator-to-configure).

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

### One package depending on another

Avoided, not forbidden. Seven of the eight optional distributions depend on
`blitzecdn` and nothing else, and that is the shape to aim for: a capability
that stands alone can be attached and detached without reasoning about anything
but core.

There is one exception, and it is declared rather than discovered.
`blitzecdn-certificates`' Automatic SSL/TLS scan probes every candidate origin
from every edge — over its current transport, and again under Full (strict) —
and upgrades only where all of them agree. That probe is `blitzecdn-origins`'
play. The scan cannot recommend anything without it, so the requirement is
written into the manifest with a pinned range:

```toml
dependencies = ["blitzecdn>=3.0.0,<4", "blitzecdn-origins>=3.0.0,<4"]
```

pip then installs both, and `blitzecdn-origins` cannot be detached out from
under the scan. The consumer still owns its port —
`blitzecdn_certificates.automatic_ssl.ports.OriginCheckRunner` describes what
the scan calls, and `composition.py` is the only module that knows
`blitzecdn-origins` satisfies it.

What is refused is the *undeclared* form: an import that works because both
happen to be installed today, and fails with an `ImportError` nothing predicted
the first time somebody detaches one.
`test_optional_packages_depend_on_each_other_only_when_they_say_so` refuses
exactly that, and `test_automatic_ssl_declares_the_origin_probe_it_runs` holds
this edge from both ends: declared and used, or neither.

A third-party runtime dependency in a capability wheel is refused outright. It
would be a dependency of the whole installation, added where nobody would look
for it.

### Ansible ownership follows capability ownership

Ansible remains the provisioning authority. What changed is *whose* tree a role
lives in.

Core owns the platform: the base host, the kernel, Docker, the firewall, the
shared edge runtime contract, the edge stack, and `blitzecdn_nginx` — the role
that renders a site's whole configuration from the merged desired-state
document. Those are not one capability's implementation.

SSH, Fail2Ban and host DNS resolution were on that list and are not any more.
All three configure the host underneath the runtime rather than the platform
the runtime needs, and a fleet whose `sshd_config` belongs to a golden image,
to a tool that predates BlitzeCDN, or to a bastion — or whose resolver belongs
to its network — had no way to decline while core's own play named the roles.
They ship as `blitzecdn-hardening` and `blitzecdn-resolver`, both attached by
default.

An optional capability owns everything that exists *because* it is installed:
its roles, its plays, its templates, its systemd units, its fleet settings and
its credentials. `blitzecdn-cache` ships `blitzecdn_cache` and `blitzecdn_cache_stats`
with the two plays that run them; `blitzecdn-certificates` ships the ACME
challenge play; `blitzecdn-geoip` ships the role that provisions the GeoLite2
database, its updater's Compose project, the systemd timer that refreshes it and
the `conf.d` snippet defining `$blitzecdn_country`; `blitzecdn-security` ships
the njs challenge module, its fleet secret and the snippet that imports it;
`blitzecdn-hardening` ships the SSH drop-in that makes an edge public-key-only,
the Fail2Ban jail in front of it, and the role that takes both off again on
decommission; `blitzecdn-resolver` ships the
systemd-resolved drop-in that decides what the host resolves names with, and
the role that takes it off again on decommission.

A package locates its own files with `core.runtime.resources.package_directory` —
never a repository-relative path, a working directory, or an editable checkout,
and never `Path(__file__)`, which is the same answer with the unpacked-
distribution check left out. Pass `__name__`: from `ansible/__init__.py` it
resolves that directory, and from `plugin.py` the package root the `nginx`
templates sit under. Then answer `blitzecdn_ansible_contributions` with what it
brought:

```python
@hookimpl
def blitzecdn_ansible_contributions() -> Sequence[AnsibleContribution]:
    return (
        AnsibleContribution(
            plugin="geoip",
            roles_path=ansible.ROLES_PATH,
            edge_roles=("blitzecdn_geoip",),
        ),
    )
```

`AnsibleContribution` has six members and each one is there because the thing
it describes is *global*.

`roles_path` answers where Ansible resolves a role name. That is the one
Ansible input that is genuinely process-wide — a single list every play
resolves against — so core has to compose it, and
`blitzecdn.core.plugins.resolution.resolve_role_search_path` is the whole of that
composition: core's directory first, then the contributions sorted by plugin
name. Deterministic, so two installations with the same packages resolve every
role identically; and a role name two contributors both ship is refused with
both names rather than silently shadowed, because Ansible takes the first match
and a package shipping `blitzecdn_nginx` would replace the edge's configuration
renderer while the deployment reported success.

`edge_roles` answers which of those roles core's edge play *runs*, which is
global for the same reason: a role nothing includes converges nothing, and the
play that would include it is core's. `resolve_edge_capability_roles` composes
that list — same ordering, and a name the contributing wheel does not actually
ship is refused here rather than by a play that is already half-way through an
edge. The list reaches Ansible as `blitzecdn_capability_roles`, an extra-var on
every run, and `src/blitzecdn/ansible/roles/blitzecdn_capabilities` is the one slot that
loops over it. It sits in the edge play's `roles:` list between
`blitzecdn_kernel` and `blitzecdn_firewall`, which is forced from both sides:
after the pre-tasks, because a capability role may need the container engine,
the runtime image and the persistent directories they establish; before
`blitzecdn_nginx`, because what a capability puts on an edge is something the
rendered configuration then depends on, and `blitzecdn_nginx` proves the whole
tree loads before the edge serves from it. Inside the roles phase rather than
as a pre-task, so a capability's `notify` reaches the fleet's *Validate and
reload Nginx* handler at the end of the play — handlers flush between
pre-tasks and roles, and a reload from there would run `nginx -t` against a
tree that had not been rendered yet.

`host_roles` is the same answer for the play's other slot, and there are two
slots because there are two answers to *when* — opposite ones. A role in the
edge slot contributes something the rendered configuration then depends on, so
it must run before `nginx -t`. A role in the host slot configures the host
underneath a runtime that is already serving, and must run after
`blitzecdn_edge_stack`: SSH policy after the firewall has been validated,
because a host that fails firewall validation must never be left key-only *and*
unreachable from the management network, and after the stack, because an edge
whose containers are all broken still has to be reachable for Ansible to repair
it. `resolve_host_capability_roles` composes it identically — same ordering,
same refusal of a name the wheel does not ship — and it reaches Ansible as
`blitzecdn_host_capability_roles`. The play uses `blitzecdn_capabilities` twice,
once per slot, with the list as a role parameter: one mechanism told where it is
standing, rather than two loops to drift apart. `blitzecdn-hardening` is the
whole of the host slot's current use, and it declares no edge role at all —
though it does declare a teardown one, because the two files it writes are
exactly the kind the next slot exists for.

`teardown_roles` is the third slot, and the only one that is not in the edge
play at all. It runs in `decommission.yml`, before `blitzecdn_teardown`, and it
exists because **core cannot remove what it may not name.** Core's teardown
role is installed on every controller, so every path in it is a path core still
knows when a capability has been detached: its own trees, the shared runtime
directories, and every systemd unit matching the managed prefix — matched
rather than listed, precisely so a unit only `blitzecdn-geoip` writes needs no
line in a core role. A file like `blitzecdn-resolver`'s drop-in under
`/etc/systemd/resolved.conf.d`, or `blitzecdn-hardening`'s SSH policy under
`/etc/ssh/sshd_config.d`, fits none of those patterns, and naming it in
core would be putting a wheel's path into a role that runs whether or not the
wheel is installed. Both of those did sit in `blitzecdn_teardown` once — the
hardening pair for longer, together with two handlers reloading `ssh` and
`fail2ban` on every decommission, installed or not.

The position is forced the same way the other two are, and by the strictest
constraint of the three: `blitzecdn_teardown` *ends* by asserting the host is
clean and failing the run if anything survived. That assertion is the verdict
on the whole decommission — the play runs while the host is still in inventory
and there is no way back to it afterwards — so a capability that withdrew its
files after it would be withdrawing them after the verdict had been passed.
Running first also leaves the state tree and the data directory still in place
for a role that needs to read them. `resolve_teardown_capability_roles` composes
the list, and it reaches Ansible as `blitzecdn_teardown_capability_roles`.

A capability declares any of the three slots, or none. Most declare none:
whatever they leave in `paths.data`, in the state tree, or in a unit matching
the managed prefix is already removed by core.

`edge_modules` is the last member, and it is Nginx's own extension point asked
the same way. `load_module` is a main-context directive, so the modules an edge
loads are one list per Nginx process — global in exactly the sense the three
role slots are, and composed by core for the same reason. A capability declares
an `EdgeModule` with the module's pkg-oss name, the shared objects to load,
whether the image has to *build* it or the official base already ships it
(`njs` is the standing example of the latter), and one directive the module
registers:

```python
AnsibleContribution(
    plugin="compression",
    roles_path=ansible.ROLES_PATH,
    edge_roles=("blitzecdn_compression",),
    edge_modules=(
        EdgeModule(
            name="brotli",
            objects=("ngx_http_brotli_filter_module.so",),
            probe="brotli off;",
        ),
    ),
)
```

`blitzecdn.core.plugins.resolution.resolve_edge_modules` composes the list — ordered by
plugin name like the slots, and refusing one module described two ways or one
shared object loaded under two names, because Nginx would take whichever was
emitted first. Two capabilities declaring the *same* module identically is
allowed and deduplicated: njs is one file in the base image, and refusing the
second would force one capability to depend on another over a file neither of
them owns.

The list reaches Ansible as `blitzecdn_nginx_modules`, on the command line with
the role slots and for the same reason, and `blitzecdn_nginx` renders it into
the edge's `load_module` file. That file is mounted over the one the *image*
was built with, and the two are deliberately different sizes. An image is built
once, pinned by digest and shared by fleets whose attached capabilities differ,
so it carries every module the published distributions declare; an edge loads
only what its controller has installed. `blitzecdn edge image spec` emits the
build arguments for the wider set from the same declarations, which is how the
Dockerfile stopped enumerating capabilities — it now takes `ENABLED_MODULES`,
`LOADED_MODULES` and `MODULE_PROBE_DIRECTIVES` as inputs and names none of
them. A capability whose module the pinned image predates is refused before
anything is rendered, by name, rather than at `nginx -t` on an unknown
directive.

#### The image is declared, never templated

`EdgeModule` is the first thing a capability needs *in the image* rather than
on the host, and it will not be the last: an apt package, a build stage, a
binary the role expects on `PATH`. Each of those extends the same shape — a
typed declaration core composes into build arguments, the way
`blitzecdn edge image spec` already composes the module set.

What a package must never contribute is Dockerfile *text*. It is the same rule
as the Nginx fragments, refused for the same three reasons: a text fragment
cannot be validated before it is built, cannot be *refused* when two packages
conflict — which is exactly what `resolve_edge_modules` does with one module
described two ways — and cannot be diffed against what the image actually
contains. Typed declaration, core renders. Hold that line even where a
fragment would be five minutes faster; the five minutes are paid back the
first time an edge boots an image nobody can explain.

#### When this contract splits

`AnsibleContribution` now carries six members — `roles_path`, `edge_roles`,
`host_roles`, `teardown_roles`, `edge_modules`, `environment_keys` — and the
trend has been about one per release. Splitting it today would cost more than
it saves: five of the six are answered together by the same three-line hookimpl,
and a package would gain three objects to return where it returns one.

The decision, written down while the reasoning is fresh: **when a seventh
member lands, split by lifecycle slot** — `EdgeContribution`,
`HostContribution`, `TeardownContribution` — because that is the seam the
members already cluster on. `edge_roles` and `edge_modules` are both "what the
edge play does"; `host_roles` is the far side of the edge being up;
`teardown_roles` is a different play altogether, and its docstring already
explains itself without reference to the other two. `roles_path` and
`environment_keys` are per-package rather than per-slot and stay on whatever
carries the set. Do not split on a smaller signal than that seventh member,
and do not add the seventh member without reading this paragraph first: the
admission test for a member is the same one the hooks use — *the thing it
describes is global, and core is the only place that can compose it*.

The lists are passed on the command line rather than written into the variables
file, because the variables file for a deployment *is* the desired-state
snapshot a rollback converges months later, and what is installed is not
desired state: pinning it would make a rollback run a capability role that has
since been detached.

A *play* is not global and is therefore not in the contract: the package that
owns one passes its path to `PlaybookRunner.run_playbook`, which is why core no
longer has a `cache_purge_playbook_path`, a `stats_playbook_path`, an
`acme_challenge_playbook_path` or an `origin_check_playbook_path` setting to
point at a file a detached package took with it. `blitzecdn-origins` is the
purest case: it contributes a role search path and *neither* slot, because its
role is reached only by its own play, on demand.

Nothing is copied or staged. The package's installed directory *is* the role,
so an upgraded package is converged from on the next run rather than from a
snapshot taken at some earlier install. There is still no runtime downloading
of roles: the wheel carried them.

#### What stays in `blitzecdn_nginx`, and why

The generic virtual-host lifecycle stays core-owned: listeners, TLS material,
ACME, origin routing, proxy headers/timeouts, status, validation and reload.
Its template exposes stable typed resource contexts (`http`, `server`,
`access`, `upstream`). Optional packages contribute named templates to those
contexts and therefore own their directives without contributing arbitrary
server blocks or executable rendering hooks. A site that needs an absent
capability is refused by name before a document
is rendered or a play starts.

Core names no module either, and that used to be the one exemption.
`blitzecdn_nginx/tasks/build-capability.yml` starts the managed edge image with
no network and no mounts and reads what it can load; the assertion beside it,
`modules-invariant.yml`, checks that output against the modules the *installed
capabilities* declared rather than against a list of its own. So the failure it
reports names the capability, the module and the image — and
`tests/architecture/test_packages.py` now carries no exemption at all to
"core's Ansible names no capability's implementation".

#### An optional capability's configuration

A capability that needs a setting does not get a field on core's `Settings`.
`under_attack_secret`, `maxmind_account_id` and `maxmind_license_key` were all
fields there, which meant every installation loaded configuration named for
distributions most of them do not have — and a capability this repository has
never heard of could not be configured at all.

Instead, `Settings.capability_environment` stages non-core `BLITZE_*` values,
merged from the process environment and the controller's `.env`, as
`SecretStr`. Each package declares its keys in
`AnsibleContribution.environment_keys`; composition rejects unknown and
duplicate ownership, then `PlaybookExecutor` forwards only resolved keys
through the environment, never `--extra-vars`.

A declaration is an `EnvironmentKey`, not a bare name:

```python
environment_keys = (
    EnvironmentKey(
        name="BLITZE_UNDER_ATTACK_SECRET",
        minimum_bytes=32,
        summary="The fleet-wide key the Under Attack Mode challenge is signed with.",
    ),
)
```

A name alone said only *whose* the key is. It said nothing about whether the
capability can work without it or what a usable value looks like, so every
package answered that half itself, in its own module and at its own moment —
which is how a placeholder signing secret came to start a controller, reach
every play, and be reported only by the first site that turned Under Attack
Mode on. Core now holds the two rules it can hold without knowing what a value
*means*, at composition, before an adapter exists:

- `required` — absence stops the control plane, naming the capability and the
  key. It is not "is this capability useful": a controller with no signing
  secret is a working control plane with one site setting it will refuse, and
  that is a deployment check's answer, not a startup failure. Most keys leave
  this alone.
- `minimum_bytes` — checked only when a value is present, so the two rules stay
  independent. For a value whose length is the whole of its validity.

Both refusals are `ConfigurationError`, not `PluginError`: nothing is wrong
with the installed package and the fix is a value the operator can change.
Anything richer — the shape of a MaxMind account id — stays the package's to
validate, because a core that could describe one would be carrying the shape of
a capability that may not be installed.

On the Python side the package reads its *own* configuration, scoped:

```python
config = platform.capability_config.for_plugin("security")
secret = config.secret(SECRET_VARIABLE)  # empty SecretStr when unset
if config.is_set(SECRET_VARIABLE):
    ...
```

`CapabilityConfig` holds the keys this package declared and no other package's.
A key it did not declare is refused rather than returned empty — the two
mistakes look identical at the call site and only one of them is a typo — and a
declared key is always present, so an unset value is an empty secret rather
than a missing entry. Reading `Settings.capability_environment` directly is the
thing this replaces: it is the whole installation's, and a package that reads
it sees every other capability's credential. See
`blitzecdn_security/config.py`.

Core's own names are excluded, which is what keeps `BLITZE_API_KEY` and
`BLITZE_API_KEYS` — the control plane's own authentication — out of every
subprocess it starts. And `blitzecdn.toml` refuses any key it does not know, so
a capability credential cannot arrive through committed configuration whatever
a package's README says.

Non-secret fleet policy is unchanged: it lives in the capability role's own
`defaults/main.yml` and is overridden with `blitzecdn config set`, exactly as
core's role defaults are. Nothing about an optional capability belongs in
`src/blitzecdn/ansible/inventory/group_vars/`, which ships inside the control plane's own
wheel and is read
by every edge whether or not the package is installed.

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
4. If it owns Ansible, put it in `src/blitzecdn_<name>/ansible/` — `roles/` for
   roles, `playbooks/` for plays, each role with its own `defaults/`,
   `templates/`, `handlers/` and `meta/argument_specs.yml` — locate it with
   `core.runtime.resources.package_directory`, and contribute it through
   `blitzecdn_ansible_contributions`. Name the role in `edge_roles` if a deploy
   should run it, and in `teardown_roles` if it writes something core's
   teardown could not find — a path outside the state tree, the data directory
   and the managed unit prefix; leave `edge_roles` empty if only the package's
   own plays
   reach it. Nothing has to be added to the justfile: the checkout's syntax and
   lint gates ask `blitzecdn ansible roles-path` and `blitzecdn ansible slots`
   for the same values the composition root resolves, and reach the roles
   themselves by glob. A declaration is the whole of the registration.
   If the capability needs a credential, give it a `BLITZE_*` name, document it
   in the package README, and read it with `lookup('env', ...)` in the role's
   defaults — and claim it in `AnsibleContribution.environment_keys` as an
   `EnvironmentKey`, with `required` and `minimum_bytes` if either applies.
   Read it on the controller through
   `platform.capability_config.for_plugin("<name>")`.
   If its Nginx resources need a dynamic module, declare it in `edge_modules`
   rather than adding it to the image build: the edge then loads it only while
   this distribution is installed, and `blitzecdn edge image spec` picks it up
   for the next published image. An edge cannot load it until an image built
   since that declaration is rolled out, and the deploy says so by name.
5. Put its tests in `packages/blitzecdn-<name>/tests/`.
6. Add the extra to `[project.optional-dependencies]` in the root
   `pyproject.toml`, the workspace source to `[tool.uv.sources]`, and the test
   path to `testpaths`.
7. `uv lock && uv sync --all-packages`.

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
  package's internals, and never on `blitzecdn.composition`, the entry-layer
  compositions,
  or the storage implementations.
* A feature may depend on core contracts, on ports it declares itself, and on
  another feature's public contract modules (`domain`, `policy`, `origins`,
  `ports`, `reporting`, `snapshots`). `sites` depends on no other feature; DNS
  and operational capabilities may depend on public `sites` contracts.
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
