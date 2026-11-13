# Architecture walkthrough

The [code map](../README.md#code-map-and-boundaries) lists what each package
owns and the rules between them. This page follows two real requests through
the code, so you can see those packages working together. The reasons behind
the larger choices are in the [decision log](decisions/README.md).

Paths below are relative to `src/blitzecdn/`. Methods are named rather than
given line numbers, so the references stay correct when a file changes.

## The pieces every request passes through

| Piece | Where | What it does |
| --- | --- | --- |
| Feature | `capabilities/<name>/feature.py` | Declares what a capability contributes: API routers, CLI commands, Ansible roles, Nginx fragments, site policy |
| Roster | `composition/roster.py` | Lists every feature module in order, with the dishka provider, rendering, operations and queued operations each one binds |
| Feature registry | `BUILTIN_REGISTRY` in `composition/roster.py` | Loads the roster's features and composes `SitePolicy` from their policy contributions, once, when `blitzecdn.composition` is imported |
| Wiring | `capabilities/<name>/wiring.py` | The capability's dishka `Provider`: builds its stores (adapters) and services, giving each service its ports |
| Bindings | `composition/bindings.py` | Binds a port to another capability's object when the dependency graph lets neither provider name the other |
| Infrastructure | `composition/infrastructure.py` | Concrete shared adapters: the database, the Ansible runner, probes, the clock. Tests swap them through `Adapters` |
| Processes | `composition/processes.py` | One dishka container per process (API, worker, CLI, installer handoff), and the worker's job handlers |
| Route / command | `capabilities/<name>/api/routes.py`, `cli.py` | Translate HTTP or CLI input into a service call and its result into a response |
| Service | `capabilities/<name>/service/` | The use case. Depends only on ports |
| Port | `capabilities/<name>/ports.py`, `core/ports/` | A `Protocol` for a collaborator: a store, a runner, a probe, another capability's service |
| Adapter | `capabilities/<name>/adapters/`, `core/ansible/`, `core/persistence/` | SQLAlchemy tables, Ansible plays, HTTP probes |

The API is assembled in `entrypoints/api/app.py`: `create_app` builds the API
container and includes every router `BUILTIN_REGISTRY.api_routers()` returns.
Routes are dishka routes (`DishkaSyncRoute`), so a handler parameter typed
`FromDishka[SiteService]` is resolved from the container that the capability's
provider populated.

## Flow 1: a record change becomes a deployed configuration

An operator turns on proxying for a record, then deploys. The edit is stored
straight away. Nothing reaches an edge until a deployment compiles, renders,
rolls out and verifies it.

### 1. The edit is stored

`POST /v3/domains/{domain}/records` and `PATCH .../records/{type}/{record}` are
in `capabilities/sites/api/routes.py`. The route turns the request model into
a domain `DnsRecord` and calls `SiteService` (`capabilities/sites/service/zones.py`).

`SiteService.create_record` and `update_record` open a Unit of Work
(`uow.transaction()`), write through the `ZoneStore` port and record a domain
event in the same transaction. `SiteProvider` in `capabilities/sites/wiring.py`
binds `ZoneStore` to the SQLAlchemy table in `capabilities/sites/adapters/persistence.py`.
See [transaction boundaries](transactions.md) for what the Unit of Work
guarantees.

A record holds no policy. Its zone holds the default `SitePolicy`
(`capabilities/sites/domain/zone.py`), and rules override it per hostname.
Validating either needs the composed site policy; see
[writing a script or a test](#writing-a-script-or-a-test).

There is no site table. A proxied record produces a serving site when hosts
are derived (`derive_hosts` in `capabilities/sites`), which happens again at
each compile.

### 2. A deployment is admitted

`POST /v3/deployments` (`capabilities/deployments/api/routes.py`) calls
`DeploymentService.submit_deployment` in
`capabilities/deployments/service/convergence.py`, inside `idempotent(...)` so
a retried request with the same `Idempotency-Key` returns the first
deployment. The key is reserved before admission and bound, through
`bind_claimed_key` (`core/ports/idempotency.py`), in the same transaction that
writes the deployment.

`submit_deployment` takes the deployment lock and calls `_queue`, which:

1. Compiles the current state with `ReleaseService.prepare` (next step), under
   the lock, so the release is the state at admission.
2. In one transaction, creates the `QUEUED` deployment row, plans its target
   edges, prunes old history, records a `deployment.queued` event and enqueues
   a `DeploymentJob` through the `QueueBackgroundRunner` port.
   `composition/infrastructure.py` binds that port to the job queue
   (`_QueuePublisher` → `JobQueue.enqueue_deployment`). The request's
   `Idempotency-Key`, if any, is bound to the deployment here too.

The route answers `202` with the deployment and a `Location` to poll; see
[queued API work](queued-operations.md).

### 3. The release is compiled and rendered

`ReleaseService` (`capabilities/releases/service/releases.py`) does this in three
parts:

- **Capture.** `capture` reads zones, records, rules and fleet settings in one
  snapshot through the `CanonicalState` port (`StoredCanonicalState` in
  `capabilities/releases/wiring.py`). Then `prepare_compilation`
  (`service/preparation.py`) derives the serving sites and asks each feature's
  rendering for its site and fleet variables. Renderings are the `rendering`
  column of the roster, bound by `ServiceProvider` in `composition/services.py`.
  Anything that can inspect the world, such as certificate stores and site
  checks, runs here and is captured as values.
- **Compile.** `compile_release` (`service/compiler.py`) is a pure function of
  the capture: equal inputs give an equal digest. A release that cannot be
  converged is still compiled and carries every finding.
- **Store and render.** `prepare` saves the release, keyed by digest, so an
  unchanged state reuses its release. `_record_configuration` then renders the
  edge configuration once on the controller with `EdgeRenderer`
  (`core/ansible/rendering.py`) and stores the bundle. See
  [controller-side rendering](decisions/0003-controller-side-rendering.md).

### 4. The worker picks it up

The worker (`entrypoints/worker.py`) builds `worker_container`, and
`WorkerProvider` (`composition/processes.py`) installs the job handlers.
`JobRunner.tick` (`capabilities/jobs/service/runner.py`) claims a job with a
lease and keeps renewing it with a heartbeat while the handler runs.
`JobQueue.run` (`capabilities/jobs/service/queue.py`) validates the payload
and calls the handler for its kind.

For a `DeploymentJob` that handler is `deployment` in `_job_handlers`, which
calls `DeploymentService.run_queued`. Under the deployment lock, `run_queued`
resumes a `RUNNING` row an earlier worker left behind, or starts a `QUEUED`
one, inside a workflow record (`_journalled`), and calls `converge`.

### 5. The rollout

`converge` checks that every planned edge is still registered, then hands over
to `Rollout.converge` (`capabilities/deployments/service/rollout.py`):

- It claims a new fence generation. Every target write carries that
  generation, so a worker that stalls past its lease cannot record results
  over a takeover.
- It publishes the release once (`publish_release` in
  `capabilities/deployments/wiring.py`): the rendered bundle, then the
  execution document beside it, where the edge play reads them.
- It takes the canary alone, then batches of `deployment_batch_size`, through
  the phases in `PHASE_ORDER`: `PREPARE`, `VALIDATE` (check mode), `STAGE`,
  `ACTIVATE`, `VERIFY`. Each Ansible phase is one `ansible-playbook` run
  limited to that batch, through the `DeploymentRunner` port, bound to
  `AnsibleRunner` in `core/ansible/runner.py`. Per-host results come from the
  Ansible Runner event stream.
- A failure stops the rollout after its batch settles. An edge that failed in
  `ACTIVATE` or `VERIFY` is rolled back (`_withdraw`); later edges are
  `SKIPPED` and keep serving what they had.

The [edge playbook](design/edge-playbook.md) design note covers pacing, the
failure policy and role order on the edge side, and
[per-edge rollout phases](decisions/0004-per-edge-rollout-phases.md) says why.

### 6. Verification

In `VERIFY`, `_verify` asks the `ServingVerifier` port (`ServingProbe` in
`capabilities/deployments/adapters/serving.py`) to request one hostname per
enabled site from the edge's public address, bypassing DNS. The request
carries a signature made with this controller's probe token, and the edge must
answer `200` naming the release digest it is serving; any other answer is a
miss. Only the edges that pass are committed
(`_commit`). An edge with no public address fails rather than passing
unverified.

### 7. The outcome is recorded

`_complete_rollout` writes the final status in one transaction: it settles
pending deployment requirements, adopts the inputs of a successful rollback,
counts the outcome for alerts and records the event. Back in the worker, the
`deployment` handler maps the status to a job outcome: `Done`, `Fail`, or
`Defer` while another deployment holds the lock.

The CLI's `blitzecdn deploy` runs the same `DeploymentService.deploy` in
process: it admits and converges under one lock and skips the queue.

## Flow 2: a queued operation (cache purge)

Purges, connection readings, origin checks, edge removal and certificate work
all follow this path. It is shorter than a deployment: no release, no rollout,
one Ansible run.

### 1. Admission

`POST /v3/cache/purge` is in `capabilities/cache/api/routes.py`. Its `submit`
closure:

1. Calls `CacheService.check_purge` (`capabilities/cache/service/cache.py`),
   which refuses hostnames no site serves. A purge that matches nothing would
   look the same as one that worked, so it is refused before anything is
   queued.
2. Builds a `CachePurgeJob` payload (`capabilities/cache/service/operations.py`)
   and submits it to `OperationService.submit`
   (`capabilities/jobs/service/operations.py`). `submit` refuses a kind no
   feature declared, and refuses a duplicate while one with the same dedupe key
   is in flight. Queued operations run once (`max_attempts=1`).

The route answers `202` with an `Operation` and `Location: /v3/operations/{id}`,
served by `capabilities/jobs/api/routes.py`.

### 2. Registration

Nothing in `jobs` knows about cache. The cache roster entry declares
`queued=lambda inputs: cache_operations.queued_operations(inputs.cache)`.
`bind_queued_operations` in `composition/roster.py` collects every entry's
operations by payload kind, refusing a kind declared twice. `ServiceProvider`
exposes them as `OperationRuns`, and `WorkerProvider` turns each into a job
handler (`_operation_handlers` in `composition/processes.py`). An operation
that needs a longer lease than the queue's default declares it in the entry's
`leases` (TLS does).

### 3. Execution

The worker claims the job as in Flow 1, step 4. The handler validates the
payload against `CachePurgeJob` and calls `purge` from `queued_operations`.
That calls `CacheService.purge_cache`, the same method the CLI's
`blitzecdn cache purge` calls directly. It:

1. Repeats `check_purge`, because a site may have gone since admission.
2. Runs the cache capability's own play through the `CacheRunner` port.
   `CachePlaybooks` (`capabilities/cache/adapters/playbooks.py`) passes the
   play path and variables to the generic `run_playbook` in
   `core/ansible/runner.py`. Core never names the cache play.
3. Builds a `PurgeResult` from the per-host results and records a
   `cache.purged` event.

The handler returns `Done(result=...)` with the report as JSON. A
`BlitzeError` becomes `Fail` with its message. The operation can succeed while
the result says `complete: false` and lists `failed_hosts`: the job ran, but
some edges may still serve the cached copy.

## Writing a script or a test

Nothing that validates or reads a site policy works until the site policy has
been composed from the features. That covers a `Domain`, a `DnsRecord`, a
`Rule`, a `CdnSite`, the API models built on them, and anything that compiles
a release. Composition happens when `blitzecdn.composition` is imported:
`BUILTIN_REGISTRY` is built at module import and seals `SitePolicy`. Without
it you get:

```text
FeatureError: the site policy is not composed yet: import `blitzecdn.composition`, ...
```

So:

- **A script or one-off tool** imports `blitzecdn.composition` before it
  builds any of those values. Getting a container from `cli_container` or
  `api_container` already imports it.
- **Tests** get this from `tests/conftest.py`, which imports
  `blitzecdn.composition` for the whole suite. A test that needs an extra
  policy contribution recomposes through `tests/support/composition.py`: the
  `policy_composition` fixture, or `temporary_policy_composition()` around
  part of a test. Its `features(...)` composes the built-in features plus
  yours, `contributions(...)` seals policy contributions directly, and
  `uncompose()` leaves the policy uncomposed. Resealing is process-wide and
  changes more than `SitePolicy._schema` (every `ComposedModel` caches a
  generated model per schema), so the helper snapshots all of it and restores
  it in a `finally`, whether the test passes, fails or composition raises.
  Never assign `SitePolicy._schema` or call `compose_registry` from a test.
- **Capability code** never imports composition (the import rules forbid it).
  It only receives already-composed values, so it never needs this.

To run a use case end to end in a test, use the production graph with test
adapters rather than constructing services by hand. `ControlPlane` in
`tests/support/control_plane.py` wraps such a container, with Ansible and the
probes replaced through `Adapters`; `tests/journeys/` uses it for the operator
path.
