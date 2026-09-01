# BlitzeCDN development tasks.
#
# `just check` runs exactly what CI runs, in the same order, so a green local
# run means a green pipeline. The CI workflow calls these recipes rather than
# repeating the commands, which is what keeps that promise true — a gate added
# here is a gate CI picks up with no second edit.
#
# Everything runs through `uv run`, so no recipe depends on a shell having the
# virtualenv activated.

set shell := ["bash", "-euo", "pipefail", "-c"]
set dotenv-load := false

# Ansible needs these to find the vendored roles and the inventory plugin.
export ANSIBLE_CONFIG := "ansible/ansible.cfg"
export ANSIBLE_LOCAL_TEMP := ".state/ansible-local"

collections := ".state/collections"

# Ansible's role search path, composed the way the control plane composes it:
# core's roles, then the roles each optional capability ships inside its own
# distribution. Spelled out here rather than globbed because `just` has no
# glob, and because a package that gains a roles directory should be a visible
# line in this file rather than a silent change in behaviour.
roles_path := "ansible/roles" + ":" + \
    "packages/blitzecdn-cache/src/blitzecdn_cache/ansible/roles"

# List the available recipes.
default:
    @just --list

# Install the whole workspace — the control plane and every optional
# capability under `packages/` — plus the development group, from the lockfile.
#
# `--all-packages` is the workspace form of `uv sync`: without it, syncing the
# root project installs `blitzecdn` alone and every optional distribution's
# tests fail to import. Development wants all of them; a server does not have
# to have any.
install:
    uv sync --frozen --all-packages
    uv run ansible-galaxy collection install -r ansible/requirements.yml -p {{collections}}

# Install exactly what a server gets: the control plane, the optional
# capabilities a BlitzeCDN installation ships with, and no development group.
#
# The extras are the attach point. `install.sh` and the container image pass
# the same two, so dropping one here is the supported way to build a controller
# without that capability — and `uv sync --frozen --no-dev` with no extras at
# all is a working control plane with neither.
install-prod:
    uv sync --frozen --no-dev --extra backup --extra cache

# The control plane on its own: no optional distribution installed at all.
#
# What `test-core-only` runs against, and the configuration the acceptance
# criteria call "BlitzeCDN root package works alone".
install-core-only:
    uv sync --frozen

# Re-resolve dependencies and update uv.lock.
lock:
    uv lock

# Fail if uv.lock is stale — a dependency edit that was never locked.
lock-check:
    uv lock --check

# Repin the uv that install.sh downloads, refreshing its checksums together.
#
# The version and the hashes have to move as one. Bumping the version alone
# leaves install.sh comparing a new download against an old hash and refusing
# to run, which looks exactly like the tampering the check exists to catch.
uv-pin version:
    #!/usr/bin/env bash
    set -euo pipefail
    for triple in x86_64-unknown-linux-gnu aarch64-unknown-linux-gnu \
                  x86_64-apple-darwin aarch64-apple-darwin; do
        sum=$(curl -fsSL --proto '=https' --tlsv1.2 \
            "https://github.com/astral-sh/uv/releases/download/{{version}}/uv-${triple}.tar.gz.sha256" \
            | awk '{print $1}')
        [[ -n "${sum}" ]] || { echo "no checksum for ${triple}" >&2; exit 1; }
        name="UV_SHA256_${triple//-/_}"
        sed -i.bak -E "s|^${name}=.*|${name}=\"${sum}\"|" install.sh
    done
    sed -i.bak -E 's|^UV_VERSION=.*|UV_VERSION="{{version}}"|' install.sh
    rm -f install.sh.bak
    echo "Pinned uv {{version}}; run 'just shell-lint' and the container install tests."

# --- the gates, in CI order ---------------------------------------------

# Format and lint every distribution in the workspace.
lint:
    uv run ruff format --check src tests packages
    uv run ruff check src tests packages

# Rewrite what `lint` would complain about.
fmt:
    uv run ruff format src tests packages
    uv run ruff check --fix src tests packages

# Strict type checking, across the whole workspace.
#
# Each distribution's `src` tree, and only those: the suite is not annotated
# and never has been, and `packages` as a bare path would have swept the
# packages' tests in while `src` leaves `tests/` out.
#
# The packages are checked against the core in *this* environment rather than
# against a published release, which is the point of the workspace: a change to
# a contract an optional capability depends on fails here rather than after a
# release.
types:
    uv run mypy src packages/*/src

# Lint the shell scripts that run as root.
shell-lint:
    uv run shellcheck install.sh \
        tests/container-install.sh tests/http3-edge-integration.sh

# Build the edge runtime image locally, the way CI and the integration test do.
#
# The tag is only a local name: production pulls the published image by the tag
# and digest recorded in the fleet settings, never a locally built one.
edge-image tag="blitzecdn-edge:dev":
    docker build --tag {{tag}} docker/edge

# Slow, privileged proof of a clean Ubuntu edge running the containerised
# runtime: real HTTP/1.1, HTTP/2, HTTP/3, GeoIP2, Brotli and Under Attack Mode,
# plus image upgrade, rollback, engine restart and both kinds of teardown.
# Deliberately separate from the normal unit suite.
test-integration-http3:
    bash tests/http3-edge-integration.sh

# `worksteal` rather than the default `load`: the suite's cost is lopsided — a
# handful of tests spawn a real Ansible and take seconds while most take
# milliseconds — and a static up-front split leaves whichever worker drew the
# Ansible tests running long after the rest have finished. Workers are separate
# processes, so every fixture here is already per-process; nothing is shared.
#
# pytest-cov combines the workers' data files itself, so the floor is measured
# against the same total a sequential run produces.
#
# The gate: the whole suite across every core, with the coverage floor.
test *args:
    uv run pytest -n auto --dist=worksteal {{args}}

# The everyday inner loop: the whole suite, in parallel, coverage off.
#
# Deselects the packaging lifecycle, which builds wheels and installs them into
# throwaway virtualenvs and costs minutes. `just test` — the gate — runs it.
test-fast *args:
    uv run pytest --no-cov -n auto --dist=worksteal -m "not packaging" {{args}}

# One optional distribution's own tests, against the workspace core.
#
#     just test-package blitzecdn-cache
test-package package *args:
    uv run --package {{package}} pytest --no-cov packages/{{package}}/tests {{args}}

# The control plane with no optional distribution installed.
#
# The other half of the boundary, and not something the normal suite can prove:
# a core test that imported an optional package would pass here only because
# the developer happened to have it installed. This syncs it away first, so the
# run really is core-only, and puts the workspace back afterwards.
#
# `tests/architecture/test_packages.py` and `test_lifecycle.py` are about the
# packages and are deselected — the lifecycle suite builds its own core-only
# environment and asserts the same property from the outside.
test-core-only *args:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'uv sync --frozen --all-packages >/dev/null' EXIT
    uv sync --frozen
    uv run pytest --no-cov -n auto --dist=worksteal tests \
        --ignore=tests/architecture/test_packages.py \
        --ignore=tests/architecture/test_lifecycle.py {{args}}

# Sequential and without coverage, because a subset would otherwise fail on the
# global coverage floor rather than on the test, and because a handful of cases
# are not worth sixteen workers.
#
# One file, or one case.
test-one *args:
    uv run pytest --no-cov {{args}}

# Where the time goes. Sequential: parallel durations overlap and mislead.
test-profile *args:
    uv run pytest --no-cov --durations=30 {{args}}

# YAML, playbook syntax, and role linting.
#
# The syntax checks use the dynamic inventory plugin in its explicit non-strict
# mode. A fresh checkout has no control-plane database, and a syntax check does
# not need hosts; production keeps using the strict inventory in
# `ansible/inventory/blitzecdn.yml`, where a missing database must stop a run.
#
# `ANSIBLE_ROLES_PATH` is composed the way the control plane composes it at run
# time: core's roles plus the roles each installed capability ships inside its
# own wheel. A package's play — the ACME challenge, the cache purge — resolves
# both its own roles and core's, exactly as a deployment does.
ansible-check:
    uv run yamllint .
    ANSIBLE_ROLES_PATH="{{roles_path}}" uv run ansible-playbook \
        -i tests/fixtures/blitzecdn.yml \
        ansible/playbooks/edge.yml --syntax-check \
        --extra-vars @tests/fixtures/desired-state.yml
    ANSIBLE_ROLES_PATH="{{roles_path}}" uv run ansible-playbook \
        -i tests/fixtures/blitzecdn.yml \
        packages/blitzecdn-certificates/src/blitzecdn_certificates/ansible/playbooks/acme-challenge.yml \
        --syntax-check
    uv run ansible-playbook -i localhost, \
        tests/integration/http3-edge.yml --syntax-check
    uv run ansible-playbook -i localhost, \
        tests/integration/http3-firewall-disabled.yml --syntax-check
    uv run ansible-playbook -i localhost, \
        tests/integration/edge-teardown.yml --syntax-check
    uv run ansible-playbook -i localhost, \
        tests/integration/docker-engine.yml --syntax-check
    uv run ansible-playbook -i localhost, \
        ansible/playbooks/control-plane.yml --syntax-check
    uv run ansible-playbook -i localhost, \
        ansible/playbooks/uninstall.yml --syntax-check
    ANSIBLE_INVENTORY=tests/fixtures/blitzecdn.yml \
    ANSIBLE_ROLES_PATH="{{roles_path}}" uv run ansible-lint \
        ansible/playbooks/edge.yml \
        ansible/playbooks/control-plane.yml ansible/playbooks/decommission.yml \
        ansible/playbooks/uninstall.yml \
        ansible/playbooks/origin-check.yml \
        packages/blitzecdn-cache/src/blitzecdn_cache/ansible/playbooks/cache-purge.yml \
        packages/blitzecdn-cache/src/blitzecdn_cache/ansible/playbooks/stats.yml \
        packages/blitzecdn-certificates/src/blitzecdn_certificates/ansible/playbooks/acme-challenge.yml \
        tests/integration/http3-edge.yml \
        tests/integration/http3-firewall-disabled.yml \
        tests/integration/edge-teardown.yml tests/integration/docker-engine.yml

# Static security analysis and a dependency vulnerability audit.
audit:
    uv run bandit -c pyproject.toml -r src packages
    uv run pip-audit

# Build every distribution in the workspace: the control plane and each
# optional capability, as independently installable wheels and sdists.
build:
    uv build --all-packages

# Fail if the published reference no longer describes this control plane.
#
# The check lives in the docs repository, because that is where the pages it
# reads live; this recipe is the direction that matters here — a route, command,
# setting, or model changed on this side and the documentation was not updated
# with it. Point `docs` at that checkout; it defaults to a sibling clone.
#
# Skips when that checkout is absent, mirroring how the check itself behaves
# when run from the docs side without this repository: a contributor who has
# cloned one of the two is not blocked by the other. CI checks both out, so the
# skip never fires there.
docs-check docs="../blitze-cdn-web":
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -d "{{docs}}" ]; then
        echo "reference check skipped: no documentation site at {{docs}}"
        exit 0
    fi
    BLITZE_CP_PATH="{{justfile_directory()}}" \
    BLITZE_CP_PYTHON="{{justfile_directory()}}/.venv/bin/python" \
        node "{{docs}}/scripts/check-api-surface.mjs" --strict

# Everything CI runs. Run this before pushing.
check: lock-check lint types shell-lint test test-core-only ansible-check audit build docs-check

# --- database -----------------------------------------------------------

# Create a migration from the difference between models.py and the database.
db-revision message:
    uv run alembic revision --autogenerate -m "{{message}}"

# --- housekeeping -------------------------------------------------------

# Run the API against the local database.
serve port="8000":
    uv run blitzecdn serve --port {{port}}

# Remove build output, caches, and coverage data. Leaves .state and .venv.
clean:
    rm -rf dist build .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov
    find src tests packages ansible -name __pycache__ -type d -prune -exec rm -rf {} +
