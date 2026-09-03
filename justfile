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
export ANSIBLE_CONFIG := "src/blitzecdn/ansible/ansible.cfg"
export ANSIBLE_LOCAL_TEMP := ".state/ansible-local"
# ansible.cfg no longer names this: it can only point inside the wheel the
# platform roles now ship in, and collections are state.
export ANSIBLE_COLLECTIONS_PATH := ".state/collections"

collections := ".state/collections"

# Ansible's role search path is deliberately not a variable here any more.
# `blitzecdn ansible roles-path` composes it from the installed distributions
# with the function the composition root calls, so the path a lint step checks
# against is the path a deployment resolves against. Spelling it out was a
# second answer to a question that already had one, and it could disagree
# with the first without anything saying so.

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
    uv run ansible-galaxy collection install -r src/blitzecdn/ansible/requirements.yml -p {{collections}}

# Install exactly what a server gets: the control plane, the optional
# capabilities a BlitzeCDN installation ships with, and no development group.
#
# The extras are the attach point. `install.sh` and the container image pass
# the same two, so dropping one here is the supported way to build a controller
# without that capability — and `uv sync --frozen --no-dev` with no extras at
# all is a working control plane with neither.
install-prod:
    uv sync --frozen --no-dev --extra backup --extra cache --extra hardening \
        --extra origins --extra resolver

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
    docker build --tag {{tag}} src/blitzecdn/docker/edge

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

# One file, or one case. Without coverage, because a subset would otherwise
# fail on the global coverage floor rather than on the test.
#
# Sequential, and measured rather than assumed — this was briefly changed to
# `-n auto` on the theory that a whole file must be faster across cores, and
# it is not. Eight workers each import the plugin tree and collect the whole
# workspace before running anything, which on this suite costs about twenty
# seconds flat:
#
#   one case                    3s sequential   26s parallel
#   tests/contract/…_contracts  22s             17s
#   tests/architecture/…cycle   286s            163s
#
# So the crossover is minutes, not seconds, and the common use of this recipe
# is the first line. For the one file where parallel genuinely wins, the
# packaging lifecycle, ask for it directly and get the workers with it:
#
#     just test-fast -m packaging
test-one *args:
    uv run pytest --no-cov {{args}}

# Where the time goes. Sequential, because parallel durations overlap and
# mislead — this is the one recipe that is deliberately not across cores.
#
# `packaging` is deselected here and not elsewhere: those cases are minutes of
# `uv build` and `uv venv` in subprocesses, they would crowd out every real
# finding in the table, and there is nothing in them to optimise anyway. Pass
# `-m packaging` to profile them regardless — a later `-m` wins.
test-profile *args:
    uv run pytest --no-cov --durations=30 -m "not packaging" {{args}}

# YAML, playbook syntax, and role linting.
#
# The syntax checks use the dynamic inventory plugin in its explicit non-strict
# mode. A fresh checkout has no control-plane database, and a syntax check does
# not need hosts; production keeps using the strict inventory in
# `src/blitzecdn/ansible/inventory/blitzecdn.yml`, where a missing database must stop a run.
#
# `ANSIBLE_ROLES_PATH` and the capability slot lists are not written out here.
# Both are asked of the control plane, which composes them from the installed
# distributions with the very functions the composition root calls at run time
# — so what this recipe checks is what a deployment would actually resolve.
#
# They used to be two literals in this file, and both had drifted. The slot
# list omitted `blitzecdn_resolver`, which declares an edge role, and named no
# teardown slot at all, so the decommission play had never been checked with a
# capability in it. Neither mistake is visible by reading: a slot is declared
# by the contributing package, and this file cannot see a declaration.
#
# A shebang recipe so both values are resolved once and shared by every command
# below, rather than reloading the plugin tree per line.
ansible-check:
    #!/usr/bin/env bash
    set -euo pipefail
    uv run yamllint .
    export ANSIBLE_ROLES_PATH="$(uv run blitzecdn ansible roles-path)"
    slots="$(uv run blitzecdn ansible slots)"
    uv run ansible-playbook \
        -i tests/fixtures/blitzecdn.yml \
        src/blitzecdn/ansible/playbooks/edge.yml --syntax-check \
        --extra-vars @tests/fixtures/desired-state.yml \
        --extra-vars "$slots"
    uv run ansible-playbook \
        -i tests/fixtures/blitzecdn.yml \
        src/blitzecdn/ansible/playbooks/decommission.yml --syntax-check \
        --extra-vars "$slots"
    uv run ansible-playbook \
        -i tests/fixtures/blitzecdn.yml \
        packages/blitzecdn-certificates/src/blitzecdn_certificates/ansible/playbooks/acme-challenge.yml \
        --syntax-check
    for play in \
        tests/integration/*.yml \
        src/blitzecdn/ansible/playbooks/control-plane.yml \
        src/blitzecdn/ansible/playbooks/uninstall.yml
    do
        uv run ansible-playbook -i localhost, "$play" --syntax-check
    done
    # Globs, not a list. Which plays and roles exist is a property of the
    # checkout, and a package that gains one should be linted because it is
    # there — not because somebody remembered to add a line here. The two
    # cache roles reached only by cache's own plays were already being linted
    # transitively; naming them by glob makes that explicit and costs nothing.
    ANSIBLE_INVENTORY=tests/fixtures/blitzecdn.yml uv run ansible-lint \
        src/blitzecdn/ansible/playbooks/*.yml \
        packages/*/src/*/ansible/playbooks/*.yml \
        packages/*/src/*/ansible/roles/*/ \
        tests/integration/*.yml

# Lint the two Dockerfiles this distribution ships.
#
# Policy and the reasoning behind every ignore live in `.hadolint.yaml`, which
# hadolint reads from the working directory — which is why the container form
# below mounts the repository and works from it rather than piping a file in.
#
# A local binary is preferred when there is one, because it is instantly
# faster and because a contributor should not need a running Docker daemon to
# lint a Dockerfile. Neither available is an error rather than a skip: a gate
# that quietly passes when its tool is missing is worse than no gate, since it
# reports green on exactly the machine that checked nothing.
docker-lint version="v2.15.1":
    #!/usr/bin/env bash
    set -euo pipefail
    files=(
        src/blitzecdn/docker/edge/Dockerfile
        src/blitzecdn/docker/control-plane/Dockerfile
    )
    if command -v hadolint >/dev/null 2>&1; then
        exec hadolint "${files[@]}"
    fi
    if ! docker info >/dev/null 2>&1; then
        echo "docker-lint needs either hadolint on PATH or a running Docker" >&2
        echo "daemon: brew install hadolint, or start Docker and re-run." >&2
        exit 1
    fi
    exec docker run --rm --volume "$PWD":/repo:ro --workdir /repo \
        "hadolint/hadolint:{{version}}" hadolint "${files[@]}"

# Static security analysis and a dependency vulnerability audit.
audit:
    uv run bandit -c pyproject.toml -r src packages
    uv run pip-audit

# Build every distribution in the workspace: the control plane and each
# optional capability, as independently installable wheels and sdists.
build:
    uv build --all-packages

# The inner-loop gate: about a minute, and it catches everything that is
# cheap to catch. Formatting, types, the shell scripts, every YAML and role,
# and the whole suite across cores with coverage off.
#
# What it deliberately leaves to `check` is only the expensive half, and each
# one is expensive for the same reason — it builds or installs something:
# `test`'s coverage pass and packaging lifecycle, `test-core-only`'s two
# workspace syncs, `audit`'s dependency fetch, and `build`. None of them is
# likely to break on an edit that this recipe passes, which is what makes
# running it instead a fair trade.
#
# Not a substitute for `check` before pushing. It is not what CI runs.
#
# `docker-lint` is left out for the same reason as the rest: without a local
# hadolint it pulls an image, and requiring a running Docker daemon for the
# inner loop would cost more than the gate catches on an edit that changes no
# Dockerfile. Run `just docker-lint` directly when one changes.
check-quick: lint types shell-lint ansible-check test-fast

# Everything CI runs. Run this before pushing.
check: lock-check lint types shell-lint test test-core-only ansible-check docker-lint audit build

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
