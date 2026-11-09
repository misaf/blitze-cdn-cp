# BlitzeCDN

BlitzeCDN is a Python control plane for CDN edge servers. It manages desired
state, compiles releases, and uses Ansible to provision and converge remote
Linux hosts running the containerized edge runtime.

The backend is a modular monolith: FastAPI and Typer expose the same application
services, Dishka assembles dependencies, and SQLAlchemy with Alembic
provide persistence. Background work uses a durable SQLite queue; no separate
message broker is required.

Collection endpoints share the [pagination contract](docs/api-pagination.md).

## Local development

Use Python 3.15.2 or later, [uv](https://docs.astral.sh/uv/), and
[just](https://just.systems/). `.python-version` makes uv pick 3.15.2, the
interpreter CI and the control-plane image run. Scripts that run on edges keep
a lower floor; see the [Ansible quick start](src/blitzecdn/assets/ansible/README.md).
Run commands from the repository root.

Docker and a global Ansible are not required: Ansible comes from the project
environment, and the checks that need Docker run in GitHub Actions. See
[testing without local infrastructure](docs/testing.md).

```sh
just setup                   # just install, then report what this machine can run
uv run blitzecdn setup       # Migrate the local database; create .env if absent
uv run blitzecdn --help
```

`install.sh` manages server lifecycle operations. Running it without arguments
shows help; for local development use the commands above.

`setup` preserves existing configuration. For a fresh checkout it creates a local
API credential in `.env` with mode `0600`. Keep that file private. Optional
controller settings are documented in
[blitzecdn.example.toml](blitzecdn.example.toml); copy it to `blitzecdn.toml` if
needed. See [configuration](docs/configuration.md) for precedence, secrets, and
capability settings. Local persistent state defaults to `.state/`.

Start the API and worker in separate terminals using the same project directory
and configuration:

```sh
just serve                  # API on port 8000; OpenAPI UI at /docs
```

```sh
uv run blitzecdn-worker      # Schedule and consume durable jobs
```

Control endpoints use `/v3`, independently of the application release version.
Legacy `/v1` and `/v2` contracts are not served by this application. `/docs`
and `/openapi.json` describe the current API; `/health` and `/metrics` remain
unversioned. See [request defaults](docs/api-defaults.md) for creation values,
PATCH semantics, inheritance, and operation scope.
See [queued API work](docs/queued-operations.md) for submission responses,
polling headers, and result handling.
Existing clients should follow the [v2 to v3 migration guide](docs/migration-v2-v3.md)
before changing their API prefix.

Control endpoints require the `X-API-Key` header; use the secret portion of the
`local:secret` entry generated in `.env`. Inspect controller state with
`uv run blitzecdn overview`. See [operations](docs/operations.md) for status,
metrics, worker heartbeats, and logs.

Inspect recent deployments with `uv run blitzecdn deployment status`, or pass a
deployment ID for its per-edge progress. Read stored hourly cache effectiveness with
`uv run blitzecdn cache stats --start ... --end ...`. These
replace the former root commands `status` and `stats`; update scripts to use
the resource groups. Controller diagnostics remain `blitzecdn overview`.

Local setup does not provision an edge. Remote operations require registered
edges, SSH access, and appropriate host privileges. Production host installation
is handled by `install.sh` and the bundled control-plane Ansible roles.

## Code map and boundaries

| Location | Responsibility |
| --- | --- |
| `src/blitzecdn/capabilities/` | Business capabilities: DNS, edges, TLS, releases, deployments, jobs, backups, and related policy |
| `src/blitzecdn/core/` | Shared domain values and ports, configuration, feature machinery, persistence infrastructure, runtime utilities, and Ansible integration |
| `src/blitzecdn/composition/` | Dependency construction, feature roster, concrete adapter bindings, and API/worker lifecycle |
| `src/blitzecdn/entrypoints/` | API and CLI assembly, worker process, and installer handoff |
| `src/blitzecdn/host/` | Host-side runtime integration |
| `src/blitzecdn/assets/` | Packaged migrations, shared Ansible automation, and Docker build assets |
| `tests/` | Capability, delivery, architecture, contract, journey, and platform checks |

Shared site identity lives in `core/domain/site.py`; `SitePolicy` in
`core/domain/site_policy.py` composes the contracts contributed by features.
These models import no capability. HTTP owns visitor headers, origins owns
origin request identity, and TLS owns transport and redirect decisions.
Composition binds their contributions before sites are validated. A caller
reads a contract through `site.policy[Contract]` and asks that contract for
its behavior, rather than making the shared site model know each capability.

Each capability contains only the parts it needs:

- `domain` and `contract`: validated values and policy without framework or I/O dependencies.
- `service`: application operations depending on explicit ports.
- `ports`: interfaces for persistence, execution, and other collaborators.
- `adapters`: concrete database, filesystem, network, or process implementations.
- `api` and `cli`: input/output translation and calls to application services.
- `settings`: configuration owned by that capability.
- `feature`: declarations of contributed routes, commands, and other features.
- `wiring`: construction and binding of capability behavior.
- `ansible` and `nginx`: capability-owned automation and configuration fragments.

Core does not import capabilities. Services do not import adapters. Delivery
code does not access persistence directly. Capabilities receive collaborators
through dependency injection and do not reach back into composition. Imports
between capabilities must follow the declared dependency graph and use the
permitted public boundaries.

These rules are executable: [.importlinter](.importlinter) checks import
direction, while [architecture tests](tests/architecture/) check capability
layout, dependency declarations, and other structural constraints.

See [transaction boundaries](docs/transactions.md) for atomic updates, nested
service calls, and coordination with external work.

Ansible template API compatibility lives in `core/ansible/templating.py`.
For upgrades, run the templating tests in `tests/platform/test_ansible_templating.py`,
rendering contracts in `tests/contract/test_configuration_rendering.py`, and
release-render tests in `tests/capabilities/releases/test_release_render.py`.

## How work runs

**Desired state:** DNS records, per-hostname policy, edge registrations, and
fleet settings are stored in the controller database. Proxied DNS records produce
serving sites; there is no separate site CRUD model. Ordinary desired-state edits
are staged until deployment. Controller configuration in TOML/environment is
separate from database-backed CDN policy.

**Releases and deployments:** The releases capability captures inputs and
compiles a release. Deployments owns convergence, locking, workflow records,
per-edge progress, and final outcomes. Rendering produces retained release
artifacts; Ansible applies the release to selected edges through the rollout
stages. Start reading at
[DeploymentService](src/blitzecdn/capabilities/deployments/service/convergence.py)
and the [release compiler](src/blitzecdn/capabilities/releases/service/compiler.py).

**API, CLI, and jobs:** CLI deployment commands run synchronously. Queued API
deployments persist admission and are executed by the worker through the same
deployment service. The worker advances schedules, claims jobs with leases and
fencing tokens, invokes registered handlers, and persists outcomes. Jobs and
workflow journals have different roles: jobs track delivery and retries;
workflows track operation checkpoints and recovery decisions. API endpoints
that call out to the CA, Ansible, DNS or HTTP probes are queued the same way
(see [queued API work](docs/queued-operations.md)); a workflow record alone
does not make an operation queued.

Process construction and lifecycle live in
[composition/processes.py](src/blitzecdn/composition/processes.py). API startup
currently performs interrupted-operation reconciliation; worker startup
registers schedules and starts reporting. Worker heartbeats indicate liveness
and do not renew job leases.

## Testing and checks

The [justfile](justfile) owns the development commands used by CI:

| Command | Use |
| --- | --- |
| `just test-one tests/capabilities/jobs/test_observability.py` | Run a focused file without the global coverage threshold |
| `just test-fast` | Run the suite in parallel, excluding packaging tests and coverage |
| `just check-quick` | Run Python/import lint, typing, shell and Ansible checks, and fast tests |
| `just check` | Run the full quality gate, including coverage, packaging tests, Dockerfile lint, security checks, and builds |
| `just test-installation` | Build and verify an isolated installed wheel |
| `just test-integration-http3` | Run the separate privileged Docker edge integration harness |
| `just doctor [task]` | Report which tasks this machine can run (`local`, `docker-lint`, `integration`, `ci`, `all`) |
| `just ci-run` | Run CI on the pushed current branch and follow it |
| `just ci-download [run]` | Download a CI run's reports and failure diagnostics |

The full test gate enforces 85% coverage. Ansible checks need the collections
installed by `just install`. Dockerfile lint requires a local `hadolint` binary
or a running Docker daemon; container integration checks require Docker and
their host privileges. Without those, push and let CI run them; see
[testing without local infrastructure](docs/testing.md), which also records what
the container evidence covers and the remaining multi-host gaps. Unit tests and
test doubles do not establish that a live edge deployment works.

## Where a change belongs

Extend the capability that owns the behavior. Keep validation and policy close
to its domain/service; introduce a port when an operation needs an external
collaborator, and implement I/O in an adapter. Add only the parts the change
needs. Shared infrastructure belongs in core when it is independent of any
capability.

Capabilities are built into the application and shipped together. External
plugins and a public Python SDK are not supported; feature contribution types
are internal composition interfaces.

For a new capability, follow the skeleton and dependency rules in
`tests/architecture/` and declare contributions in `feature.py`. Routes and
commands then reach the API and CLI through the feature registry, and the
capability builds its own stores and services in a dishka `Provider` in its
`wiring.py`. A new capability touches:

- [composition/roster.py](src/blitzecdn/composition/roster.py): a
  `RosterEntry` for its feature module, with its provider and its rendering,
  operations and queued-operation binders if it has any.
- An Alembic migration under `src/blitzecdn/assets/migrations/versions/` for
  any new tables (see below).

Its provider asks for its own ports. When another capability's object
satisfies one and the declared capability graph does not let either provider
name the other, [composition/bindings.py](src/blitzecdn/composition/bindings.py)
binds the port to it.

Keep API and CLI handlers thin and test their shared behavior through the
service.

A per-site setting belongs to a capability's contract: a `CapabilityPolicy`
model in its `contract.py`, contributed as a `PolicyContribution` in
`Feature.site_policy`. The site policy, the zone and rule patches, and the
`/v3/domains` and `/v3/hosts` schemas are composed from those contributions
when the registry loads, so the setting needs no edit in `dns` or core.
A rule that reads another contract is a contributed check with that contract in
`depends_on`; contracts are read as `policy[TlsPolicy]`.

Persisted schema changes need an Alembic migration under
`src/blitzecdn/assets/migrations/`; `just db-revision "description"` generates a
candidate for review. The initial revisions (files `NNNN_initial_<capability>.py`,
revision IDs `NNNN_<capability>`) create each capability's tables in one
dependency-ordered chain; `tests/platform/test_migrations.py` lists that chain
and every later revision, and checks the tables each one creates. Revisions,
initial or later, are permanent: add a new one for each later schema change
rather than editing or folding into an existing revision, and keep application
imports out of them. Apply migrations
with controller services stopped using `uv run blitzecdn migrate`.

Public CLI, HTTP, database, and Ansible surfaces are frozen under
`tests/contract/frozen/`. For an intentional public change, update delivery
parity coverage where applicable, run `just refreeze`, and review the generated
diff. For edge configuration changes, update the Python policy and Ansible/Nginx
implementation together and run the relevant contract tests.
