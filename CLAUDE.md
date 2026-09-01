# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Tasks run through `just` (recipes are in `justfile`) and every recipe wraps `uv run`, so no shell needs the virtualenv activated.

```bash
just install       # uv sync --frozen --all-packages + the Ansible collections into .state/
just check         # every CI gate, in CI order — run before pushing
just test-fast     # the whole suite across every core, coverage off — the inner loop
just test-one tests/features/sites/test_domain.py -k some_case   # a single case
just test-package blitzecdn-cache   # one optional distribution's own tests
just test-core-only                 # the suite with no optional package installed
just build                          # every wheel and sdist in the workspace
```

`just check` is the contract with CI: `.github/workflows/ci.yml` calls these same recipes rather than repeating commands, so a gate added to the justfile is a gate CI picks up with no second edit. Don't add a CI step that bypasses it.

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
- **Ansible needs two env vars** — `ANSIBLE_CONFIG=ansible/ansible.cfg` and `ANSIBLE_LOCAL_TEMP=.state/ansible-local`. The justfile exports both; a manual `ansible-playbook` invocation must too. `-i` is deliberately omitted: `ansible.cfg` points at the `blitzecdn` dynamic inventory plugin, which reads the fleet from the control-plane database.
- **Optional capabilities are attached with extras, not dependencies.**
  `pip install blitzecdn` installs neither `blitzecdn-backup` nor
  `blitzecdn-cache`; `install.sh` and the container image pass
  `--extra backup --extra cache`, and `BLITZECDN_CAPABILITIES` overrides that
  list. Keep `backup` on any controller that will be updated in place —
  `install.sh update` takes a database backup before it changes anything.
- **`just docs-check`** validates this control plane against the published reference in the sibling `../blitze-cdn-web` checkout. It skips when that directory is absent; CI checks it out, so it never skips there. A new route, CLI command, setting, env var, or model needs a counterpart on the docs side.
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
│   │   ├── sites/          #   domain.py composes every contract; policy/ its own
│   │   └── dns/  edges/  deployments/  diagnostics/  maintenance/
│   ├── core/               # shared runtime and infrastructure implementations:
│   │   │                   # SQLite, Ansible, Certbot, filesystem, process, config
│   │   └── plugins/        # hookspecs, discovery, manager, registry
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
│   ├── blitzecdn-http3/    #   QUIC listener state; HTTP/1.1 and /2 stay core
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
  inheritance into one flat frozen model that the v1/v2 schemas, the persisted
  policy JSON, the deployment snapshots and every edge role consume, so none of
  those four can leave.
- **An optional package composes itself.** `bootstrap.py` builds the *required*
  services and knows nothing about what is installed beside it. A package builds
  its service in its own `composition.py` from what the control plane publishes:
  `platform.settings`, `platform.events`, `platform.sites` (the read side of the
  site model, typed as a port) and `platform.fleet` (a `PlaybookRunner`: run
  this named play with these variables against these hosts, and nothing
  feature-shaped). Core's `AnsibleRunner` therefore has no `run_cache_purge`
  any more — building a purge document is the cache package's business.
- **A package owns its Ansible too.** The roles and plays that exist *because* a
  capability is installed ship inside its wheel, under
  `src/blitzecdn_<name>/ansible/{roles,playbooks}/`, located with
  `importlib.resources` — never a repository-relative path or a working
  directory, both of which are absent on an installed controller. `roles/` is
  contributed through `blitzecdn_ansible_contributions`, and
  `core/ansible/roles.py` composes the one global input Ansible has: core's
  roles first, then contributions sorted by plugin name, refusing a role name
  two packages both ship rather than letting the first match shadow the other.
  A *play* is passed by path to `run_playbook`, so it needs no hook and core
  keeps no setting naming it. What stays core-owned is the platform — the base
  host, Docker, the firewall, the edge runtime contract and `blitzecdn_nginx`,
  which renders whatever the merged desired-state document holds. An absent
  capability contributes no variables and its block is simply not there;
  splitting that template per capability would mean concatenating configuration
  text through a hook, which this architecture refuses.
- **One top-level package is one capability.** A strategy, a protocol version, a mode or a single switch is not one: `capability → feature internals → strategy/mode/option`. gzip and Brotli are `CompressionMode` values inside `compression`; HTTP/3 is a switch in `http`; Under Attack Mode is a switch in `security`; certificate issuance and the Automatic SSL/TLS scan are `tls/certificates` and `tls/automatic_ssl`. `test_no_strategy_mode_or_option_becomes_a_top_level_feature` refuses a `features/gzip`, `features/http3`, `features/certificates` or `features/under_attack` package by name. That rule is about the *feature tree* only: an optional distribution is named after the implementation an operator attaches, which is why `blitzecdn-certificates` and `blitzecdn-http3` are wheels while those feature directories stay refused. HTTP/1.1 and HTTP/2 are baseline and never optional — there is no `blitzecdn-http1` or `blitzecdn-http2`. Before adding a feature, answer **which existing capability owns this?** — a new top-level package is the exception.
- **Each capability splits into a contract and an implementation.** `policy.py` (or `policy/`) holds pure configuration values and imports only `core` and other capabilities' contracts. Everything else consumes `CdnSite` and therefore sits above the contract layer. `sites/domain.py` composes the four borrowed contracts into the flat `CdnSite` and owns only the rules that read across two capabilities at once. Two declared graphs enforce this — `ALLOWED_FEATURE_DEPENDENCIES` and `ALLOWED_POLICY_DEPENDENCIES` — plus the layer rule that makes them compose: **a contract never imports an implementation.**
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
firewall rule kind. Keep the frozen HTTP v1
*resource* representations — sites, records, edges — in `api/v1_models.py` and
evolve v2 independently in `api/v2_models.py`; the *operational* ones (runs,
deployments, drift, workflows) are identical in both versions and live
once in `api/operations.py`, with `api/requests.py` for the bodies. An optional
capability's operational shapes are its own — `PurgeResult` and
`CacheStatsReport` are in `blitzecdn_cache/api/models.py`, built on core's
`OperationModel` and `as_operation`, because core cannot carry a shape for a
capability that may not be installed. A version
that has to diverge from a shared shape defines its own class with the version
in the name (`CdnSiteV2`) rather than editing the shared one — pydantic
disambiguates a name collision by qualifying *both* sides with their module
path, so a second `Deployment` would rename the other version's published
schema. Persisted deployment snapshots are a
versioned compatibility contract; add an upcaster and a legacy fixture before
changing their shape. Feature services own their small policy dataclasses
and receive those rather than the global `Settings` model. A plugin reads
intervals off `platform.settings` at registration only.

**`core/database.py`, `core/database_engine.py` and `core/database_models.py` stay where they are.** They look like they belong under `core/persistence/` beside the stores, but the committed `0001_initial_schema.py` names `blitzecdn.core.database_models.UtcDateTime()` fifteen times and `migrations/script.py.mako` imports the same module into every migration it generates. Moving the module breaks that migration at import time, which breaks fresh installs and the forward-migrate step of `backup restore`; the only ways out are editing a historical migration or leaving a shim, and both are worse than the current names. `core/persistence/` holds the stores, `core/database*` holds the engine and the mapped rows they sit on — that split is already the real seam.

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

**Do not edit the tracked files under `ansible/inventory/group_vars/`.** Fleet-wide non-secret Ansible policy lives in the control-plane database — change it with `blitzecdn config set/list/unset`, and the dynamic inventory plugin publishes it to every edge. Those tracked files are shipped defaults only.

Secrets stay in `.env` (auto-loaded by the CLI as local defaults; already-exported variables win). Runtime state — SQLite data, generated Ansible vars, locks, run logs — lives under `.state/` and is gitignored.

## Operational invariants

These constrain what a change is allowed to do:

- One deployment at a time, via a filesystem lock. Each run uses an immutable desired-state snapshot.
- Failed and timed-out runs stay recorded and never become a canonical rollback target. Rollback converges the selected snapshot first; canonical desired state changes only after Ansible succeeds.
- Startup republishes queued records and marks orphaned running records `abandoned` **only while holding the deployment lock**.
- SQLite plus local locking means a single control-plane node. Active/active would require replacing both with transactional shared infrastructure — don't design around it as if it already works.
- Every long-running BlitzeCDN process is a dedicated Compose service and that service's main process. Never start Uvicorn or Dramatiq with `docker exec`, background shells, or host application systemd units. One-off commands use the installed `blitzecdn` wrapper, which runs `docker compose run --rm blitzecdn-cli`.
- `install.sh` runs as root, so it is linted like the Python (`just shell-lint`). `just uv-pin <version>` is the only correct way to bump the uv it downloads: the version and its four checksums must move together, or the installer compares a new download against an old hash and refuses to run.
