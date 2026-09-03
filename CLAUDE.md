# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Tasks run through `just` (recipes are in `justfile`) and every recipe wraps `uv run`, so no shell needs the virtualenv activated.

```bash
just install       # uv sync --frozen --all-packages + the Ansible collections into .state/
just check-quick   # lint, types, shell-lint, ansible-check, test-fast — ~2 min, the inner loop
just check         # every CI gate, in CI order — ~8 min, run once before pushing
just test-fast     # the whole suite across every core, coverage off
just test-one tests/features/sites/test_domain.py -k some_case   # a single case
just test-package blitzecdn-cache   # one optional distribution's own tests
just test-core-only                 # the suite with no optional package installed
just build                          # every wheel and sdist in the workspace
```

`just check` is the contract with CI: `.github/workflows/ci.yml` calls these same recipes rather than repeating commands, so a gate added to the justfile is a gate CI picks up with no second edit. Don't add a CI step that bypasses it.

`just check-quick` is not that contract and is not what CI runs — it is the same gates minus the expensive half (coverage, the packaging lifecycle, the two core-only syncs, `audit` and `build`). Work against it, then run `just check` once before pushing. Reach for the full gate earlier only when `pyproject.toml` or `uv.lock` changed, since `lock-check`, `audit` and `build` are the gates that then have something to say.

**Run a subset sequentially.** `just test-one` is deliberately not parallel: eight xdist workers each import the plugin tree and collect the whole workspace first, which costs about twenty seconds flat — a single case measures 3s sequential against 26s parallel. The crossover is minutes, so only the packaging lifecycle is worth `just test-fast -m packaging`.

Things that are not guessable:

- **This is a uv workspace, not one distribution.** The root project is
  `blitzecdn`; every directory under `packages/` is an optional capability that
  builds and installs as its own wheel. `uv sync` installs the control plane
  *alone*, which is why `just install` passes `--all-packages` — a plain sync
  leaves `packages/*/tests` unable to import and looks like a broken checkout.
  `just test-core-only` uses the plain sync deliberately, and syncs the packages
  back on the way out.

- **`--no-cov` when running a subset.** `pyproject.toml` sets `--cov-fail-under=85` globally, so any run narrower than the full suite fails on coverage rather than on the test. `just test-one` already passes it.
- **The suite runs in parallel.** `just test` — the gate, and what `just check` calls — is `pytest -n auto --dist=worksteal`, and pytest-cov combines the workers' data, so the floor is measured against the same total a sequential run produces. Workers are separate processes and every fixture is per-test, so a test that needs a port binds `:0`, a test that needs a path takes `tmp_path`, and a test that shells out to Ansible gives the child its own `ANSIBLE_LOCAL_TEMP`. A new test that reaches for a fixed port, a fixed path outside `tmp_path`, or a shared temp directory breaks under `-n` and not sequentially.
- **`filterwarnings = ["error"]`.** A new DeprecationWarning anywhere in the dependency tree fails the suite.
- **`BLITZECDN_UPDATE_FIXTURE=1 pytest tests/test_contract.py --no-cov`** regenerates the control-plane/edge contract fixture. Do this only after an *intended* change to `CdnSite` or the Nginx role.
- **Contract tests skip silently when the Ansible collections aren't installed.** Check the outcome, not the exit code: a `tests/test_contract.py` run that reports skips rather than passes means the collections are missing, and it exits `0` either way. Run `just install` (or `./install.sh`) first.
- **Ansible needs three env vars** — `ANSIBLE_CONFIG=src/blitzecdn/ansible/ansible.cfg`, `ANSIBLE_LOCAL_TEMP=.state/ansible-local` and `ANSIBLE_COLLECTIONS_PATH=.state/collections`. The justfile exports all three; a manual `ansible-playbook` invocation must too. The last two are not in `ansible.cfg`: since the platform's Ansible moved inside the package, a relative path there resolves into site-packages, and both name state. `-i` is deliberately omitted: `ansible.cfg` points at the `blitzecdn` dynamic inventory plugin, which reads the fleet from the control-plane database.
- **Optional capabilities are attached with extras, not dependencies.**
  `pip install blitzecdn` installs neither `blitzecdn-backup` nor
  `blitzecdn-cache`; `install.sh` and the container image pass
  `--extra backup --extra cache`, and `BLITZECDN_CAPABILITIES` overrides that
  list. Keep `backup` on any controller that will be updated in place —
  `install.sh update` takes a database backup before it changes anything.
- **`just lock-check`** fails when `pyproject.toml` and the committed `uv.lock` have drifted. Editing dependencies means running `just lock`.

Work happens on `3.x`, not `master`.

## Architecture

A **plugin-first, capability-oriented modular monolith** with **ports and adapters** inside each feature, **`pluggy` for registration**, and an **explicit composition root**, **enforced by tests, not by convention** — `tests/architecture/test_layering.py` walks the real source tree and will fail your change if you cross a boundary. Read it, and [PLUGINS.md](PLUGINS.md), before restructuring anything.

```
blitze-cdn-cp/
├── pyproject.toml          # the `blitzecdn` distribution + [tool.uv.workspace]
├── src/blitzecdn/
│   ├── features/           # one package per REQUIRED product or operational
│   │   │                   # capability, with only the layers it actually needs
│   │   ├── compression/    #   policy.py — gzip and Brotli are strategies
│   │   ├── http/           #   policy.py + plugin.py — baseline HTTP/1.1 + /2,
│   │   │                   #   the http3_enabled switch, listener contract
│   │   ├── security/       #   policy.py + plugin.py — firewall, Under Attack Mode
│   │   ├── tls/            #   policy.py + certificates/ + automatic_ssl/
│   │   ├── sites/          #   domain.py composes every contract; policy/ its own;
│   │   │                   #   service.py owns the site — canonical, not derived
│   │   │                   #   — behind ports.py + persistence.py + api/ + cli.py.
│   │   │                   #   `dns` writes one column of it: server_names
│   │   └── dns/  edges/  deployments/  diagnostics/  maintenance/
│   ├── core/               # shared runtime and infrastructure implementations:
│   │   │                   # SQLite, Ansible, Certbot, filesystem, process, config
│   │   └── plugins/        # hookspecs, discovery, manager, registry
│   ├── ansible/            # the PLATFORM's Ansible, inside the package so it
│   │                       # ships in the wheel: roles/, playbooks/, the
│   │                       # dynamic inventory plugin, ansible.cfg and the
│   │                       # shipped group_vars. Located with
│   │                       # importlib.resources, never from project_dir
│   ├── docker/             # the image build inputs, in the wheel for the same
│   │                       # reason: edge/ is a whole build context and
│   │                       # control-plane/ is a Dockerfile whose context is
│   │                       # the source tree, so `blitzecdn.docker` publishes
│   │                       # no constant for that context
│   ├── api/                # FastAPI entry point
│   ├── cli/                # Typer entry point
│   ├── bootstrap.py        # the sole production composition root
│   ├── scheduler.py        # APScheduler entry point
│   └── worker.py           # Dramatiq entry point
├── packages/               # OPTIONAL capabilities: real, separate wheels
│   ├── blitzecdn-backup/   #   archive and restore the control plane's state
│   ├── blitzecdn-cache/    #   purge, and cache-effectiveness reporting —
│   │                       #   ansible/ carries both roles and both plays
│   ├── blitzecdn-certificates/  # issuance, renewal, Automatic SSL/TLS
│   ├── blitzecdn-compression/   # the gzip and Brotli implementation
│   ├── blitzecdn-geoip/    #   visitor country lookup: the BZ-IPCountry header
│   │                       #   and country firewall rules both need it
│   ├── blitzecdn-hardening/ #  the edge *host's* front door: public-key-only
│   │                       #   SSH and Fail2Ban. No site setting asks for it
│   ├── blitzecdn-http3/    #   QUIC listener state; HTTP/1.1 and /2 stay core
│   ├── blitzecdn-origins/  #   probing site origins *from the edges*: role,
│   │                       #   play, `origin check`, POST /v{1,2}/origins/check
│   ├── blitzecdn-resolver/ #   host DNS an edge can trust — and the only user
│   │                       #   of the decommission slot, because its drop-in
│   │                       #   is at a path core must not name
│   └── blitzecdn-security/ #   site firewall and Under Attack validation
└── tests/                  # core behavior, plugin contracts, architecture,
                            # cross-package contracts, integration
```

The rules that shape it:

- **A capability is required, optional, or not a capability.** A *required*
  capability is a package under `features/` listed in `BUILTIN_PLUGINS`; its
  failure is fatal. An *optional* capability is a distribution under `packages/`
  found only through its `blitzecdn.plugins` entry point; installing it makes
  the capability appear and removing it makes it disappear, with no line of core
  edited either way. A strategy, a mode or a switch is neither. **The core
  distribution must never import an optional package and must never ask whether
  one is installed** — `tests/architecture/test_packages.py` refuses both, and
  `tests/architecture/test_lifecycle.py` builds the real wheels and proves the
  cycle. Before extracting anything, read the "Why the site-policy capabilities
  are built in" section of [PLUGINS.md](PLUGINS.md): `CdnSite` composes
  `CompressionPolicy`, `ProtocolPolicy`, `SecurityPolicy` and `TlsPolicy` by
  inheritance into one flat frozen model that the published schemas, the persisted
  policy JSON, the deployment snapshots and every edge role consume, so none of
  those four can leave.
- **An optional package composes itself.** `bootstrap.py` builds the *required*
  services and knows nothing about what is installed beside it. A package builds
  its service in its own `composition.py` from what the control plane publishes:
  `platform.settings`, `platform.events`, `platform.sites`
  (`features/sites/ports.py`'s `SiteReader` — list and get, and nothing that
  writes), `platform.site_editor` (the `SiteService` itself, for the one package
  that genuinely writes a site: `blitzecdn-certificates` narrows it to the two
  methods it calls with a `SiteEditor` port of its own),
  `platform.origin_probe` and `platform.fleet` (a
  `PlaybookRunner`: run this named play with these variables against these
  hosts, and nothing feature-shaped). Core's `AnsibleRunner` therefore has no
  `run_cache_purge` or `run_origin_check` any more — building a purge document
  is the cache package's business and building a probe document is
  `blitzecdn-origins`'. A package **may** depend on another package when it
  genuinely needs one, and then it says so in its `pyproject.toml` with a
  pinned range: the Automatic SSL/TLS scan runs `blitzecdn-origins`' play, so
  `blitzecdn-certificates` declares it. An *undeclared* cross-package import is
  refused — `test_optional_packages_depend_on_each_other_only_when_they_say_so`
  — because detaching one would then break the other with an ImportError
  nothing predicted. A third-party runtime dependency in a capability wheel is
  refused outright: it is a dependency of the whole installation, added where
  nobody would look for it.
- **A package owns its Ansible too, and so does core.** Everything that exists
  on an edge *because* a capability is installed — roles, plays, templates,
  systemd units, fleet settings, credentials — ships inside its wheel under
  `src/blitzecdn_<name>/ansible/{roles,playbooks}/`, located with
  `importlib.resources` — never a repository-relative path or a working
  directory, both of which are absent on an installed controller. The platform's
  own Ansible keeps the same contract from the same module shape:
  `src/blitzecdn/ansible/` holds the eleven platform roles, the four plays, the
  dynamic inventory plugin and `ansible.cfg`, and `blitzecdn/ansible/__init__.py`
  publishes `ROLES_PATH`, `EDGE_PLAYBOOK`, `DECOMMISSION_PLAYBOOK` and
  `INVENTORY_PLUGINS_PATH` that `Settings` reads. It used to resolve them from
  `project_dir`, which made the checkout an undeclared runtime dependency of the
  root wheel; `tests/architecture/test_lifecycle.py` now installs core alone into
  a virtualenv and refuses that. What must *not* go in there is state: a relative
  `collections_path` or `local_tmp` in `ansible.cfg` would resolve inside
  site-packages, so both are set from `Settings.state_dir` at run time and
  exported by the justfile and `install.sh` for a bare `ansible-playbook`.
  `blitzecdn_ansible_contributions` carries five things, and each is there
  because the thing it describes is *global*: `roles_path`, which
  `core/plugins/resolution.py` composes into the one process-wide search path
  Ansible has (core first, then contributions sorted by plugin name, refusing a
  role name two packages both ship rather than letting the first match shadow
  the other); and `edge_roles`, the roles core's edge play runs, resolved the
  same way and reaching Ansible as the `blitzecdn_capability_roles` extra-var
  that `src/blitzecdn/ansible/roles/blitzecdn_capabilities` loops over. That slot sits
  between `blitzecdn_kernel` and `blitzecdn_firewall` in the `roles:` list —
  late enough to have the engine and the runtime directories, early enough that
  `blitzecdn_nginx` proves the whole tree loads afterwards, and inside the
  roles phase so a capability's `notify` reaches the reload handler at the end
  of the play rather than between pre-tasks and roles. `host_roles` is the
  same question for the play's *second* slot, reaching Ansible as
  `blitzecdn_host_capability_roles` and resolved by
  `resolve_host_capability_roles`; the play uses the one
  `blitzecdn_capabilities` role twice, with the list as a role parameter and
  `allow_duplicates` making the second invocation deliberate. That slot sits
  *after* `blitzecdn_edge_stack`, and the reason is the opposite of the edge
  slot's: a role there configures the host underneath a runtime that is already
  serving, and must not run earlier — SSH policy before firewall validation is
  how a host ends up key-only *and* unreachable, and an edge whose containers
  are all broken must still be reachable for Ansible to repair it.
  `blitzecdn-hardening` is the whole of that slot's use. `teardown_roles` is
  the third slot and the only one outside the edge play: it reaches Ansible as
  `blitzecdn_teardown_capability_roles`, is resolved by
  `resolve_teardown_capability_roles`, and sits in `decommission.yml` *before*
  `blitzecdn_teardown`, whose closing clean-host assertion is the verdict on
  the whole decommission and must not be passed before half the removal has
  happened. It exists because core cannot remove what it may not name: a file
  at a path only a wheel knows — `blitzecdn-resolver`'s drop-in under
  `/etc/systemd/resolved.conf.d` is the case — would otherwise sit in a role
  that is installed whether or not that wheel is, and a host is usually
  decommissioned by a controller whose package set has drifted from the one
  that converged it. What core still removes on its own is what core wrote: its
  own trees, the shared runtime directories, and every systemd unit matching
  the managed prefix, matched rather than listed for the same reason.
  `edge_modules` is the fifth, and it asks the same question about Nginx's own
  extension point: `load_module` is a main-context directive, so the dynamic
  modules an edge loads are one list per process. `resolve_edge_modules` (in
  `core/plugins/resolution.py`, with every other resolver over contributions)
  composes it, it reaches Ansible as `blitzecdn_nginx_modules`,
  and `blitzecdn_nginx` renders it over the list the *image* was built with.
  The two differ on purpose — the image is pinned by digest and shared by
  fleets whose capabilities differ, so it carries the superset and each edge
  loads its own subset — and `blitzecdn edge image spec` emits that superset as
  the image's build arguments from the same declarations. That is why
  `src/blitzecdn/docker/edge/` now names no capability: it used to enumerate
  `geoip2`, `brotli` and `njs`, which left a detached capability's module
  loading on every edge until a new image was published. All these
  lists travel on the command line, never in the variables file: that file *is*
  the desired-state snapshot a rollback converges later, and what is installed
  is not desired state. A *play* is passed by path to `run_playbook`, so it needs
  no hook and core keeps no setting naming it. What stays core-owned is the
  platform — the base host, Docker, the firewall, the edge runtime contract and
  `blitzecdn_nginx`, which renders whatever the merged desired-state document
  holds. SSH and Fail2Ban are *not* platform and left with
  `blitzecdn-hardening`; host DNS resolution is not either and left with
  `blitzecdn-resolver`. All three configure the host, not the runtime, and a
  fleet whose `sshd_config` or whose resolver belongs elsewhere had no way to
  decline while core's play named them. The renderer exposes typed `http`, `server`, `access` and `upstream`
  resource contexts; packages contribute named templates, never complete
  server blocks or arbitrary hook output. GeoIP, compression, HTTP/3, cache and
  request-security directives therefore live with their distributions while
  core retains the generic server/TLS/proxy skeleton. A site needing an absent
  capability is refused by name before a play starts. The runtime contract's
  `paths.data` and `paths.modules` are how a capability hands the edge a file:
  core creates and mounts both read-only and never learns what is in them.

- **An optional capability's configuration is not a field on `Settings`.**
  `Settings.capability_environment` stages non-core `BLITZE_*` values from the
  environment and `.env`, each as `SecretStr`. Installed plugins explicitly
  claim names through `AnsibleContribution.environment_keys`, and a claim is an
  `EnvironmentKey` rather than a bare string: it carries `required`, a
  `minimum_bytes` for a value whose length is the whole of its validity, and a
  `summary` an operator is shown. Composition rejects unknown or duplicate
  claims, refuses a missing required key or a too-short value with a
  `ConfigurationError` naming the capability, and `PlaybookExecutor` forwards
  only the resolved subset through the environment, never `--extra-vars`. The
  owning package's role reads its own name with `lookup('env', ...)`; on the
  Python side it reads `platform.capability_config.for_plugin("<name>")`, a
  `CapabilityConfig` holding *its own* declared keys and no other package's —
  never `Settings.capability_environment`, which is the whole installation's.
  Anything richer than presence and length stays the package's to validate.
  Core names are excluded,
  which is what keeps `BLITZE_API_KEY` out of every subprocess. Non-secret
  fleet policy lives in the capability role's own `defaults/main.yml` and is
  overridden with `blitzecdn config set`; nothing about an optional capability
  belongs in `src/blitzecdn/ansible/inventory/group_vars/`.
- **One top-level package is one capability.** A strategy, a protocol version, a mode or a single switch is not one: `capability → feature internals → strategy/mode/option`. gzip and Brotli are `CompressionMode` values inside `compression`; HTTP/3 is a switch in `http`; Under Attack Mode is a switch in `security`; certificate issuance and the Automatic SSL/TLS scan are `tls/certificates` and `tls/automatic_ssl`. `test_no_strategy_mode_or_option_becomes_a_top_level_feature` refuses a `features/gzip`, `features/http3`, `features/certificates` or `features/under_attack` package by name. That rule is about the *feature tree* only: an optional distribution is named after the implementation an operator attaches, which is why `blitzecdn-certificates` and `blitzecdn-http3` are wheels while those feature directories stay refused. HTTP/1.1 and HTTP/2 are baseline and never optional — there is no `blitzecdn-http1` or `blitzecdn-http2`. Before adding a feature, answer **which existing capability owns this?** — a new top-level package is the exception.
- **Each capability splits into a contract and an implementation.** `policy.py` (or `policy/`) holds pure configuration values and imports only `core` and other capabilities' contracts. Everything else consumes `CdnSite` and therefore sits above the contract layer. `sites/domain.py` composes the four borrowed contracts into the flat `CdnSite` and owns only the rules that read across two capabilities at once. **A site is canonical**: `sites/service.py` creates, edits and deletes one, `sites/persistence.py` behind `sites/ports.py` stores it, and the `site` commands and `/v{1,2}/sites` routes write it. A DNS record is a *pointer* — it either answers with an address of its own or names the site that answers for its hostname — and the one thing `dns` still writes on a site is `server_names`, the set of hostnames routed to it, through its own `SiteHostnames` port. That is why the `dns → sites` arrow survived the inversion pointing the same way while meaning something much smaller. The rule that replaced "the projection has no write side" is its mirror: **nothing in `sites` may write `server_names`** — not `SitePatch`, not `SiteService` — because that column has a writer outside the feature and a site write carrying hostnames would silently revert a record change made between the read and the write. Two declared graphs enforce this — `ALLOWED_FEATURE_DEPENDENCIES` and `ALLOWED_POLICY_DEPENDENCIES` — plus the layer rule that makes them compose: **a contract never imports an implementation.**
- **Features own their business logic.** A feature's `domain.py` (plus every `policy` module, `origins.py`, and `snapshots.py`) is pure: no I/O, no framework, no adapter package (fastapi, typer, sqlite3, subprocess, ansible, dns, cryptography, yaml). DNS owns records and may derive sites from public contracts, never the reverse.
- **The consumer owns the port.** A feature's `ports.py` holds the narrow `Protocol` interfaces *that feature* calls. A port belongs to whoever calls it, never to whoever implements it — a port describing a run some other feature performs is what forces a cycle, and `test_no_feature_port_declares_another_feature_s_playbook` refuses it.
- **Concrete infrastructure implements those ports.** `core/` and a feature's own `adapters/` supply the implementations. A feature *service* (`service.py`, `rollback.py`, `reporting.py`) names its ports and never a concrete adapter.
- **`bootstrap.py` is the production composition root.** It builds every adapter, injects it into the services, loads the plugins, and hands each plugin the finished control plane. Production wiring exists nowhere else, and it is the only module that knows one `AnsibleRunner` satisfies `CacheRunner`, `EdgeRunner`, `DeploymentRunner` and `DeploymentLocker` alike. The order is the architecture: **adapters → services → plugins → contributions**, and nothing flows back.
- **Pluggy registers; it never carries business calls.** A feature's `plugin.py` contributes routers, CLI groups, scheduled jobs, health checks, desired-state fragments, deployment checks and lifecycle work. Business communication stays an explicit call on a constructor-injected service — `certificate_service.issue(...)`, never `hook.issue_certificate(...)`. There is no global plugin manager and no service lookup in a request path; `platform.<service>` appears in `plugin.py` and nowhere else, and a layering test refuses it anywhere else.
- **`api/app.py` and `cli/main.py` import no feature.** Both ask the plugin registry. Adding an extension-style feature means writing its `plugin.py` and adding one line to `BUILTIN_PLUGINS`; a separately installed distribution needs neither, only an entry point in the `blitzecdn.plugins` group.
- **Desired state is contributed, not centralised.** `DesiredStateRenderer` frames the document and knows nothing about what goes in it: `sites` projects the site model, `http` states the fleet's baseline listener stance, the optional `blitzecdn-http3` overrides it with the QUIC requirement and its single `reuseport` listener owner, and `tls` overrides the two certificate paths. Contributions are typed and merged order-independently — two plugins writing one variable is an error unless exactly one declares it in `overrides`. Never concatenate configuration text through a hook.
- **A CDN switch is not automatically a feature — it belongs to the capability that owns its behavior.** Compression, HTTP protocol, security and TLS policy live with their capabilities. Cache policy, visitor headers and origin identity stay under `sites/policy/` because nothing outside a site's own configuration reads them: the `cache` capability owns purge and statistics *operations*, and moving `CachePolicy` under it would make `sites` depend on a feature that already depends on `sites`. Keep edge runtime/build capability separate.
- **Cross-feature dependencies follow the declared graph.** See `ALLOWED_FEATURE_DEPENDENCIES` below.
- **Entry layers (`api/`, `cli/`) never touch persistence or an adapter directly.** They call services on `ControlPlane`.

**Do not introduce a top-level `domain/`, `application/`, or `infrastructure/` package.** The layer-first structure was deliberately replaced by the feature-first one above, and `test_legacy_layer_first_packages_have_no_source_modules` fails the suite if one comes back. Business logic goes in the owning feature, not in a global layer.

Equally, do not reach for a generic repository, a DI container, a command bus or mediator, CQRS, or event sourcing. None of them has a demonstrated need here, and each one trades a boundary the tests can check for indirection they cannot. Do not build anything on top of pluggy either: ten hookspecs and one registry are the whole mechanism, and a hook with one hypothetical future consumer is not a hook.

Adapter document shapes do not belong on domain models. Keep Ansible and
inventory mappings in `core/ansible/mapping.py` — and keep them free of any one
capability's names: a nested policy block that should be *absent* from the edge
document rather than present and empty subclasses
`core.validation.OmittedWhenEmpty`, and `site_to_ansible` prunes by that
declaration. `core/validation.py` is for primitives two or more capabilities
share; a table with one consumer (the ISO country list, the HTTP-method shape)
belongs beside the contract that validates against it, and
`tests/architecture/test_packages.py` fails a `core/` module that names a
firewall rule kind. **The HTTP API publishes one version.** The *resource*
representations — sites, records, zones, edges — are in `api/models.py`, the
*operational* ones (runs, deployments, drift, workflows) in `api/operations.py`
with `api/requests.py` for the bodies, and each feature contributes a single
`api/routes.py` under the `/v1` prefix. There was a frozen v1 beside a live v2,
which cost two copies of every router, two model modules and a projection that
kept the frozen half from breaking when the domain grew — insurance against
clients that do not exist. Nothing published has shipped, so a shape change is
an edit. Should a real second version ever be needed, it arrives as a new
`api/routes_v2.py` and its own model module, and the *class names* are what
must then diverge: pydantic disambiguates a name collision by qualifying
*both* sides with their module path, so a second `Deployment` would rename the
first one's published schema as a side effect. An optional capability's
operational shapes are its own — `PurgeResult` and `CacheStatsReport` are in
`blitzecdn_cache/api/models.py`, built on core's `OperationModel` and
`as_operation`, because core cannot carry a shape for a capability that may not
be installed. A deployment snapshot carries the zones, their records and the sites,
and its `schema_version` is a discriminator rather than a compatibility layer:
there is one version, a document that is not it is refused, and a shape change
is an edit rather than an upcaster. Feature services own their small policy dataclasses
and receive those rather than the global `Settings` model. A plugin reads
intervals off `platform.settings` at registration only.

**There is one Alembic revision, and a schema change edits it.** Nothing is installed anywhere, so `0001_initial_schema.py` is the schema rather than a historical artefact: change the models, change that file, and `tests/platform/test_migrations.py` holds the two to each other and refuses a second revision. A migration chain starts existing the day something ships.

**`core/database.py`, `core/database_engine.py` and `core/database_models.py` stay where they are.** They look like they belong under `core/persistence/` beside the stores, but `0001_initial_schema.py` names `blitzecdn.core.database_models.UtcDateTime()` fifteen times and `migrations/script.py.mako` imports the same module into every migration it generates. Moving the module breaks that migration at import time, which breaks fresh installs and the forward-migrate step of `backup restore`; the only ways out are editing a historical migration or leaving a shim, and both are worse than the current names. `core/persistence/` holds the stores, `core/database*` holds the engine and the mapped rows they sit on — that split is already the real seam.

Rules that the layering tests exist to defend, each guarding a failure that is invisible in review:

- **`ControlPlane` exposes no `repository`, `database`, or `audit_log` attribute**, and the entry layers may not reference those names at all. Reads go through a service or a port exactly like writes — a read endpoint calling a store directly is the regression this refuses.
- **`ControlPlane` forwards no calls.** Entry layers reach the owning service (`control_plane.dns.create_record(...)`); adding a passthrough method that restates a service signature is a step backwards.
- **The API must not construct adapters.** `create_app` asks the composition root for a `ControlPlane` rather than building one, so the adapter choice stays in a single place.
- **Never reason from Ansible's textual output.** Raw output is retained per run for operators only; the control plane decides from structured Runner events (`core.runs.AnsibleRun`) and nothing else. Reading `.stdout`/`.stderr` or matching on `PLAY RECAP` in a feature's domain or service modules fails the suite. `core/filesystem.py` owns the one reader; a service may reach it through the `LogReader` port to quote a log into a message.
- **Feature-to-feature dependencies are declared, not discovered.** `ALLOWED_FEATURE_DEPENDENCIES` in `tests/architecture/test_layering.py` is the graph, enforced in both directions and required to stay acyclic. A new edge is a decision you write down. The usual way one appears by accident is a port: a feature declaring a run that another feature performs forces that feature to import this one, which is how four cycles got in. Each feature declares the slice it calls (`CacheRunner`, `EdgeRunner`, `DeploymentRunner`, `DeploymentLocker`), and `control_plane.FleetRunner` is the only place that knows one `AnsibleRunner` satisfies them all.
- **No outbox pattern and no `ThreadBackgroundRunner`.** Both were removed; a test refuses their return by name. There is one queue (Dramatiq over Redis), and two actors: one deployment, one scheduled job. A job's *name* travels in the message and the worker resolves it against the plugin registry, so a plugin can contribute recurring work without an actor being declared for it.
- **Every feature has a `plugin.py`, and every `plugin.py` is in `BUILTIN_PLUGINS`.** Both directions are tested: a feature the plugin manager never hears of contributes no routes and no commands, and the failure is silent absence rather than an error. An optional package is the mirror image: it must **not** be in `BUILTIN_PLUGINS`, it declares `required=False`, and its entry point is the whole of how it is found. Registering one both ways collides on its own name at startup.
- **A configuration may depend on an optional capability, and then its absence is fatal.** `required_capabilities` — a `Settings` field, so `BLITZE_REQUIRED_CAPABILITIES` or a `blitzecdn.toml` key, **not** `blitzecdn config set`, which manages fleet Ansible policy — lists capability *tokens*; `ControlPlane` refuses to start while one is unprovided, naming it and listing what is installed. The tokens come from configuration and the answer from `PluginMetadata.provides` — there is no `if feature == "cache":` anywhere, and a capability this repository has never heard of is checked identically. Absence *without* such a declaration is silent and correct: detaching is a supported operation.

## Configuration

Precedence, most specific first: CLI arguments (one invocation only) → `BLITZE_*` environment variables → inventory/group/host vars (environment policy) → namespaced role defaults (non-secret implementation defaults). `blitzecdn.toml` supplies non-secret defaults and env vars override it; secrets are deliberately not valid TOML keys.

**Do not edit the tracked files under `src/blitzecdn/ansible/inventory/group_vars/`.** Fleet-wide non-secret Ansible policy lives in the control-plane database — change it with `blitzecdn config set/list/unset`, and the dynamic inventory plugin publishes it to every edge. Those tracked files are shipped defaults only.

Secrets stay in `.env` (auto-loaded by the CLI as local defaults; already-exported variables win). Runtime state — SQLite data, generated Ansible vars, locks, run logs — lives under `.state/` and is gitignored.

## Operational invariants

These constrain what a change is allowed to do:

- One deployment at a time, via a filesystem lock. Each run uses an immutable desired-state snapshot.
- Failed and timed-out runs stay recorded and never become a canonical rollback target. Rollback converges the selected snapshot first; canonical desired state changes only after Ansible succeeds.
- Startup republishes queued records and marks orphaned running records `abandoned` **only while holding the deployment lock**.
- SQLite plus local locking means a single control-plane node. Active/active would require replacing both with transactional shared infrastructure — don't design around it as if it already works.
- Every long-running BlitzeCDN process is a dedicated Compose service and that service's main process. Never start Uvicorn or Dramatiq with `docker exec`, background shells, or host application systemd units. One-off commands use the installed `blitzecdn` wrapper, which runs `docker compose run --rm blitzecdn-cli`.
- `install.sh` runs as root, so it is linted like the Python (`just shell-lint`). `just uv-pin <version>` is the only correct way to bump the uv it downloads: the version and its four checksums must move together, or the installer compares a new download against an old hash and refuses to run.
