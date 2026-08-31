# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Tasks run through `just` (recipes are in `justfile`) and every recipe wraps `uv run`, so no shell needs the virtualenv activated.

```bash
just install       # uv sync --frozen + install the Ansible collections into .state/
just check         # every CI gate, in CI order — run before pushing
just test-fast     # the whole suite across every core, coverage off — the inner loop
just test-one tests/test_domain.py -k some_case        # a single case
```

`just check` is the contract with CI: `.github/workflows/ci.yml` calls these same recipes rather than repeating commands, so a gate added to the justfile is a gate CI picks up with no second edit. Don't add a CI step that bypasses it.

Things that are not guessable:

- **`--no-cov` when running a subset.** `pyproject.toml` sets `--cov-fail-under=85` globally, so any run narrower than the full suite fails on coverage rather than on the test. `just test-one` already passes it.
- **The suite runs in parallel.** `just test` — the gate, and what `just check` calls — is `pytest -n auto --dist=worksteal`, and pytest-cov combines the workers' data, so the floor is measured against the same total a sequential run produces. Workers are separate processes and every fixture is per-test, so a test that needs a port binds `:0`, a test that needs a path takes `tmp_path`, and a test that shells out to Ansible gives the child its own `ANSIBLE_LOCAL_TEMP`. A new test that reaches for a fixed port, a fixed path outside `tmp_path`, or a shared temp directory breaks under `-n` and not sequentially.
- **`filterwarnings = ["error"]`.** A new DeprecationWarning anywhere in the dependency tree fails the suite.
- **`BLITZECDN_UPDATE_FIXTURE=1 pytest tests/test_contract.py --no-cov`** regenerates the control-plane/edge contract fixture. Do this only after an *intended* change to `CdnSite` or the Nginx role.
- **Contract tests skip silently when the Ansible collections aren't installed.** Check the count, not the exit code — thirty-one tests, not thirty-one skips. Run `just install` (or `./install.sh`) first.
- **Ansible needs two env vars** — `ANSIBLE_CONFIG=ansible/ansible.cfg` and `ANSIBLE_LOCAL_TEMP=.state/ansible-local`. The justfile exports both; a manual `ansible-playbook` invocation must too. `-i` is deliberately omitted: `ansible.cfg` points at the `blitzecdn` dynamic inventory plugin, which reads the fleet from the control-plane database.
- **`just docs-check`** validates this control plane against the published reference in the sibling `../blitze-cdn-web` checkout. It skips when that directory is absent; CI checks it out, so it never skips there. A new route, CLI command, setting, env var, or model needs a counterpart on the docs side.
- **`just lock-check`** fails when `pyproject.toml` and the committed `uv.lock` have drifted. Editing dependencies means running `just lock`.

Work happens on `3.x`, not `master`.

## Architecture

A hexagonal layering that is **enforced by tests, not by convention** — `tests/test_layering.py` walks the real source tree and will fail your change if you cross a boundary. Read it before restructuring anything.

- `domain/` — pure. Imports nothing but itself: no I/O, no framework, no adapter package (fastapi, typer, sqlite3, subprocess, ansible, dns, cryptography, yaml).
- `application/ports/` — feature-owned `Protocol` interfaces over the outside world; may see the domain. Ports are deliberately narrow, so each service's constructor documents its true reach into persistence.
- `application/` — orchestrates the domain through ports and never names a concrete adapter.
- `infrastructure/` — SQLite, Ansible, Certbot, filesystem, DNS. Implements application ports without depending on application services.
- `control_plane.py` — the sole composition root. It builds every adapter and injects it into the services; production wiring exists nowhere else.
- `cli/`, `api/` — entry points, split along matching feature boundaries. They call services on `ControlPlane`.

Adapter document shapes do not belong on domain models. Keep Ansible and
inventory mappings in `infrastructure/ansible_mapping.py`. Keep the frozen HTTP
v1 representations in `api/v1_models.py` and `api/v1_operations.py`, and evolve
v2 independently in `api/v2_models.py` and `api/v2_operations.py`. Persisted deployment snapshots are a
versioned compatibility contract; add an upcaster and a legacy fixture before
changing their shape. Application services own their small policy dataclasses
and receive those rather than the global `Settings` model.

Rules that the layering tests exist to defend, each guarding a failure that is invisible in review:

- **`ControlPlane` exposes no `repository`, `database`, or `audit_log` attribute**, and the entry layers may not reference those names at all. Reads go through a service or a port exactly like writes — a read endpoint calling a store directly is the regression this refuses.
- **`ControlPlane` forwards no calls.** Entry layers reach the owning service (`control_plane.dns.create_record(...)`); adding a passthrough method that restates a service signature is a step backwards.
- **The API must not import `infrastructure`.** `create_app` asks the composition root rather than constructing a control plane, so the adapter choice stays in one place.
- **Never reason from Ansible's textual output.** Raw output is retained per run for operators only; the control plane decides from structured Runner events (`domain.runs.AnsibleRun`) and nothing else. Reading `.stdout`/`.stderr` or matching on `PLAY RECAP` in `domain/` or `application/` fails the suite. `infrastructure/filesystem.py` owns the one reader; `application` may reach it through the `LogReader` port to quote a log into a message.
- **No outbox pattern and no `ThreadBackgroundRunner`.** Both were removed; a test refuses their return by name. There is one queue (Dramatiq over Redis).

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
