# Working in this repository

BlitzeCDN is a control plane for containerised Nginx edges: Python decides, Ansible
converges. This file is the orientation an agent needs before its first edit. It does
not restate [README.md](README.md) (what the product does), [PLUGINS.md](PLUGINS.md)
(how a wheel contributes), or [COMPATIBILITY.md](COMPATIBILITY.md) (what a version
number obliges) — read those when the task is about one of them.

## The shape of the checkout

A `uv` workspace. The root distribution is `blitzecdn`; ten optional capabilities live
under `packages/`, each its own wheel with its own `tests/` beside it.

```
src/blitzecdn/
  core/          ports, domain, persistence, config, plugin machinery, the Ansible substrate
  capabilities/  the built-in slices: dns, edges, tls, http, security, cache, deployments, …
  composition/   the composition root: ControlPlane, Repository, the scheduler
  api/  cli/     delivery
  ansible/       roles, playbooks, the inventory plugin — see "Ansible" below
  migrations/    one revision, deliberately
packages/blitzecdn-*/  optional capabilities, same internal shape
tests/           the control plane's suite; support modules on pythonpath
docs/decisions/  ADRs
```

A capability — built-in or packaged — is `domain/`, `service/`, `adapters/`, `api/`,
and optionally `policy` and `cli`. Those four are **directories, not files**:
`tests/architecture/packages/test_package_layout.py` refuses `service.py` in a package,
because the layering rules key off the directory and a file spelling escapes them.

## Commands

Everything goes through `just`; every recipe runs under `uv run`, so nothing depends on
an activated virtualenv. `.github/workflows/ci.yml` calls these recipes rather than
repeating them, which is what makes a green local `check` mean a green pipeline.

```bash
just check-quick   # the inner loop, ~2 min — lint, types, shell, ansible, the suite
just check         # everything CI runs, ~8-9 min — before pushing, not per edit
just test-one tests/path/test_thing.py::test_case
just test-package blitzecdn-cache
just test-core-only    # the suite with no optional wheel installed
```

Run `check-quick` while working. Run the full `check` once, before pushing. What `check`
adds is only the expensive half — the coverage pass, the packaging lifecycle, two
workspace syncs, `pip-audit`, `docker-lint`, `build` — none of which is likely to break
on an edit that `check-quick` passes.

Integration (`just test-integration-http3`) is privileged and slow, and does not
reproduce under local Docker Desktop; it runs on the remote host.

## What the architecture tests enforce

These are executable, in `tests/architecture/`, and reading them beats guessing.

- **Two graphs.** A capability's `policy` is its *contract*: pure values, importing only
  `core` and other contracts. Everything else is its *implementation*. The layer rule is
  `core < contracts < implementations`, and a contract may never import an
  implementation. This is what lets `dns` compose every capability's contract into one
  `CdnSite` without the graph becoming a cycle.
- **Domain and contracts import no I/O.** Not fastapi, typer, sqlalchemy, subprocess,
  ansible, cryptography, dnspython, yaml, redis or dramatiq — those live in `adapters/`.
- **No new top-level capability for a strategy.** `http3`, `certificates`, `geoip`,
  `under_attack` and a dozen more are named in `_STRATEGIES_OWNED_BY_A_CAPABILITY` and
  mapped to the capability that owns them. A protocol version, a mode, or an
  implementation choice goes *inside* a capability. If you are about to create one, the
  question to answer first is "which capability owns this?".
- **Core works alone.** `just test-core-only` proves it. A core test that imports an
  optional package passes only on a machine that happens to have it installed.
- **Cross-capability imports** reach `domain`, `policy`, `ports`, `reporting` and
  nothing else.

## Frozen surfaces

`tests/contract/frozen/` holds the published surfaces — HTTP routes and schemas, the CLI,
the plugin ABI, the SDK, the Ansible roles and variables, the database schema. Anything
outside this repository can bind to them.

Do not edit those files to make a red test green. Regenerate with `just refreeze` only
when a change to a public surface is *intended*, and let the diff be its own reviewable
act. After the first release, changing one is a version decision.

## Types

`just types` is `mypy --strict` over `src`, every package's `src`, and the suite's
**shared helper modules** — `tests/*.py` and `packages/*/tests/*_support.py`. The test
cases themselves are not annotated and are out of scope.

The helpers are in the gate because that is where the doubles live, and a double is a
claim about a `Protocol` somebody else declared. `FakeRunner`, `FakeEdgeStore` and the
background queues stand in for core's ports; `FakePreflight` and `_RecordingIssuer` stand
in for the certificates package's. Each helper module states its conformance in an
`if TYPE_CHECKING:` block at the end, and `just types` is what holds it — a double that
drifts from its port fails in the file where the double is, with the signature diff.

Because test *cases* are outside that scope, **a `# type: ignore` written in a test case
suppresses nothing**. If you find one, it is inert; the question is whether the claim it
was making is true, not whether to keep the comment. An ignore inside a helper module is
the opposite — `strict` checks those, so one that stops being needed fails as unused.

## Test conventions

- **Import by name.** No `from support import *`. The star imports were removed
  deliberately: they hid where a symbol came from, and they blinded ruff — eight real
  `S603` findings were invisible because it could not resolve `subprocess` through one.
- Shared helpers are on `pythonpath` (see `[tool.pytest.ini_options]`), imported as
  `from control_plane_fixtures import ...`, not by adjacency.
- Fixtures are re-exported through a `conftest.py` with `__all__`, never imported into a
  test module where a same-named parameter would shadow them.
- `-n auto --dist=worksteal`, because the suite's cost is lopsided. Do not re-tune the
  worker count.
- Coverage floor is 85%, measured across the whole workspace.

## Ansible

The roles under `src/blitzecdn/ansible/roles/` are **substrate**: they serve every
capability. Only optional wheels own capability-specific roles, in their own
`ansible/roles/`. Do not move core's roles into `capabilities/`.

The role search path and the capability slot lists are never written out as literals —
`just ansible-check` asks the control plane for them with the same functions the
composition root calls, so what gets linted is what a deployment resolves. Both were
literals once and both had silently drifted.

## Domain facts worth knowing before you model anything

- **Sites are derived, not stored.** Zones and rules own policy; `CdnSite` is composed by
  `derive_hosts` and has no table. Do not add one.
- **A rule is a match plus overrides, first match wins** — the winning rule contributes
  everything and no other rule does.
- **No backward compatibility yet.** Nothing is installed anywhere, so schema changes edit
  the single migration in `src/blitzecdn/migrations/versions/`. Do not write upcasters or
  a second revision.

## Versions

One number lives in twenty-three places — core, ten wheels, their dependency bounds, two
edge image pins, the lockfile. `just release-version X.Y.Z` moves all of them. Never edit
a version string by hand; the pins that were edited by hand are exactly the ones that
drifted.

## Where rationale goes

The code in this repository explains itself at length, and that is intentional — comments
here carry the argument, not a restatement of the line below. Match that density when you
edit; a terse patch in a verbose file reads as unfinished.

Rationale local to one function stays in its docstring. When reasoning constrains several
files, or explains why the arrangement a reader would expect was rejected, write a record
in `docs/decisions/` and end it with the *Code and verification* section the existing
three use. A record describing an arrangement the code no longer has is worse than no
record.
