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
    for target in x86_64 aarch64; do
        triple="${target}-unknown-linux-gnu"
        sum=$(curl -fsSL --proto '=https' --tlsv1.2 \
            "https://github.com/astral-sh/uv/releases/download/{{version}}/uv-${triple}.tar.gz.sha256" \
            | awk '{print $1}')
        [[ -n "${sum}" ]] || { echo "no checksum for ${triple}" >&2; exit 1; }
        sed -i.bak -E "s|^UV_SHA256_${target}=.*|UV_SHA256_${target}=\"${sum}\"|" install.sh
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

# Lint the shell scripts that run as root or get piped into a shell.
shell-lint:
    uv run shellcheck install.sh bootstrap.sh tests/container-install.sh

# The test suite, with the coverage floor from pyproject.
test *args:
    uv run pytest {{args}}

# YAML, playbook syntax, and role linting.
#
# The syntax checks run against the real dynamic inventory. They used to name
# `hosts.example.yml`, which was deleted when the fleet moved into the database
# — so these two steps had been failing in CI ever since, on a missing file
# rather than on anything about the playbooks. An empty fleet is fine here: a
# syntax check parses the play, it does not need a host to target.
ansible-check:
    uv run yamllint .
    uv run ansible-playbook -i ansible/inventory/blitzecdn.yml \
        ansible/playbooks/edge.yml --syntax-check \
        --extra-vars @tests/fixtures/desired-state.yml
    uv run ansible-playbook -i ansible/inventory/blitzecdn.yml \
        ansible/playbooks/acme-challenge.yml --syntax-check
    uv run ansible-lint \
        ansible/playbooks/edge.yml ansible/playbooks/acme-challenge.yml \
        ansible/playbooks/control-plane.yml ansible/playbooks/decommission.yml \
        ansible/playbooks/cache-purge.yml ansible/playbooks/stats.yml

# Static security analysis and a dependency vulnerability audit.
audit:
    uv run bandit -c pyproject.toml -r src
    uv run pip-audit

# Build the wheel and the source distribution.
build:
    uv build

# Everything CI runs. Run this before pushing.
check: lock-check lint types shell-lint test ansible-check audit build

# --- database -----------------------------------------------------------

# Show the schema revision the local database is on.
db-current:
    uv run blitzecdn db current

# Migrate the local database to the newest revision.
db-upgrade:
    uv run blitzecdn db upgrade

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
    find src tests -name __pycache__ -type d -prune -exec rm -rf {} +
