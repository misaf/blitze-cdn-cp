# BlitzeCDN

BlitzeCDN is a Python control plane for CDN edge servers. It manages desired
state, compiles releases, and uses Ansible to provision and converge remote
Linux hosts running the containerized edge runtime.

The backend is a modular monolith: FastAPI and Typer expose the same application
services, Dishka assembles dependencies, and SQLModel/SQLAlchemy with Alembic
provide persistence. Background work uses a durable SQLite queue; no separate
message broker is required.

## Local development

Use Python 3.12 or later, [uv](https://docs.astral.sh/uv/), and
[just](https://just.systems/). CI runs the main quality gate on Python 3.14 and
compatibility tests on 3.12 and 3.13. Run commands from the repository root.

```sh
just install                 # Locked Python dependencies and pinned Ansible collections
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

Control endpoints require the `X-API-Key` header; use the secret portion of the
`local:secret` entry generated in `.env`. Inspect controller state with
`uv run blitzecdn overview`. See [operations](docs/operations.md) for status,
metrics, worker heartbeats, and logs.

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
workflows track operation checkpoints and recovery decisions. Some operations,
including certificate issuance, still execute synchronously; a workflow record
alone does not make an operation queued.

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

The full test gate enforces 85% coverage. Ansible checks need the collections
installed by `just install`. Dockerfile lint requires a local `hadolint` binary
or a running Docker daemon; container integration checks require Docker and
their host privileges. Unit tests and test doubles do not establish that a live
edge deployment works.

## Where a change belongs

Extend the capability that owns the behavior. Keep validation and policy close
to its domain/service; introduce a port when an operation needs an external
collaborator, and implement I/O in an adapter. Add only the parts the change
needs. Shared infrastructure belongs in core when it is independent of any
capability.

For a new capability, follow the skeleton and dependency rules in
`tests/architecture/`, declare contributions in `feature.py`, and register it in
[composition/roster.py](src/blitzecdn/composition/roster.py). Bind concrete
dependencies in composition and the capability's `wiring.py`. Keep API and CLI
handlers thin and test their shared behavior through the service.

Persisted schema changes need an Alembic migration under
`src/blitzecdn/assets/migrations/`; `just db-revision "description"` generates a
candidate for review. Apply migrations with controller services stopped using
`uv run blitzecdn migrate`.

Public CLI, HTTP, database, and Ansible surfaces are frozen under
`tests/contract/frozen/`. For an intentional public change, update delivery
parity coverage where applicable, run `just refreeze`, and review the generated
diff. For edge configuration changes, update the Python policy and Ansible/Nginx
implementation together and run the relevant contract tests.
