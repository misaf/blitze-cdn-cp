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

# List the available recipes.
default:
    @just --list

# Install the project and its development group from the lockfile.
install:
    uv sync --frozen
    uv run ansible-galaxy collection install -r ansible/requirements.yml -p {{collections}}

# Install exactly what a server gets: no development group.
install-prod:
    uv sync --frozen --no-dev

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

# Format and lint the Python.
lint:
    uv run ruff format --check src tests
    uv run ruff check src tests

# Rewrite what `lint` would complain about.
fmt:
    uv run ruff format src tests
    uv run ruff check --fix src tests

# Strict type checking.
types:
    uv run mypy src

# Lint the shell scripts that run as root.
shell-lint:
    uv run shellcheck install.sh tests/container-install.sh

# The test suite, with the coverage floor from pyproject.
test *args:
    uv run pytest {{args}}

# YAML, playbook syntax, and role linting.
#
# The syntax checks use the dynamic inventory plugin in its explicit non-strict
# mode. A fresh checkout has no control-plane database, and a syntax check does
# not need hosts; production keeps using the strict inventory in
# `ansible/inventory/blitzecdn.yml`, where a missing database must stop a run.
ansible-check:
    uv run yamllint .
    uv run ansible-playbook -i tests/fixtures/blitzecdn.yml \
        ansible/playbooks/edge.yml --syntax-check \
        --extra-vars @tests/fixtures/desired-state.yml
    uv run ansible-playbook -i tests/fixtures/blitzecdn.yml \
        ansible/playbooks/acme-challenge.yml --syntax-check
    ANSIBLE_INVENTORY=tests/fixtures/blitzecdn.yml uv run ansible-lint \
        ansible/playbooks/edge.yml ansible/playbooks/acme-challenge.yml \
        ansible/playbooks/control-plane.yml ansible/playbooks/decommission.yml \
        ansible/playbooks/uninstall.yml \
        ansible/playbooks/cache-purge.yml ansible/playbooks/stats.yml \
        ansible/playbooks/origin-check.yml

# Static security analysis and a dependency vulnerability audit.
audit:
    uv run bandit -c pyproject.toml -r src
    uv run pip-audit

# Build the wheel and the source distribution.
build:
    uv build

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
check: lock-check lint types shell-lint test ansible-check audit build docs-check

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
    find src tests ansible -name __pycache__ -type d -prune -exec rm -rf {} +
