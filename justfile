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
export ANSIBLE_CONFIG := "src/blitzecdn/assets/ansible/ansible.cfg"
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

# Install the application and development tools, then pinned Ansible collections.
install: && collections
    uv sync --frozen

# The third-party collections the roles are converged with, pinned exactly by
# `requirements.yml`.
#
# Needed by anything that *runs* a role rather than reading it: a deployment,
# and the role-contract tests, which resolve modules through
# `ANSIBLE_COLLECTIONS_PATH`. Without these a role fails to parse, which reads
# as a broken role rather than a missing dependency.
collections:
    uv run ansible-galaxy collection install -r src/blitzecdn/assets/ansible/requirements.yml -p {{collections}}

# The complete application without development tools.
install-prod:
    uv sync --frozen --no-dev

# Re-resolve dependencies and update uv.lock.
lock:
    uv lock

# Fail if uv.lock is stale — a dependency edit that was never locked.
lock-check:
    uv lock --check

# Copy the blitzecdn_docker role's packages, apt source and daemon.json into
# install.sh's generated block.
#
# The installer is downloaded on its own and has to install Docker before the
# image that runs the role exists, so it carries a copy. Edit the role, then run
# this; the suite fails while the copy is stale.
installer-sync:
    uv run python tests/installer_sync.py

# Move the application version, both edge image pins and the control-plane
# image pin together. Publishing the corresponding images remains an explicit
# release action: the tag does it.
release-version version:
    #!/usr/bin/env bash
    set -euo pipefail
    python3 - <<'PY'
    import pathlib
    import re
    import sys

    version = "{{version}}"
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit("error: expected MAJOR.MINOR.PATCH, got %r" % version)
    major = version.split(".")[0]
    following = str(int(major) + 1)


    def reversion(text):
        return re.sub(r'(?m)^version = ".*"$', 'version = "%s"' % version, text, count=1)


    core = pathlib.Path("pyproject.toml")
    core.write_text(reversion(core.read_text()))

    pins = {
        "src/blitzecdn/assets/ansible/roles/blitzecdn_edge/defaults/main.yml": (
            r"(blitzecdn_edge_runtime_image_default: .*blitzecdn-edge):\S+",
            r"\1:%s" % version,
        ),
        "src/blitzecdn/assets/ansible/inventory/group_vars/blitzecdn_edges/defaults.yml": (
            r'(?m)^blitzecdn_edge_image_tag: ".*"$',
            'blitzecdn_edge_image_tag: "%s"' % version,
        ),
        "src/blitzecdn/assets/ansible/roles/blitzecdn_controlplane/defaults/main.yml": (
            r"(?m)^(blitzecdn_controlplane_image: .*blitzecdn-control-plane):\S+$",
            r"\1:%s" % version,
        ),
        # The release a downloaded install.sh installs.
        "install.sh": (
            r"(?m)^(readonly CONTROL_PLANE_IMAGE=.*blitzecdn-control-plane):\S+$",
            r"\1:%s" % version,
        ),
    }
    for name, (pattern, replacement) in pins.items():
        path = pathlib.Path(name)
        text, count = re.subn(pattern, replacement, path.read_text())
        if count != 1:
            sys.exit("error: %s: expected one image pin, rewrote %d" % (name, count))
        path.write_text(text)
    PY
    uv lock
    echo "Moved to {{version}}. Commit, then tag v{{version}} to publish both images."

# --- the gates, in CI order ---------------------------------------------

# Format and lint every distribution in the workspace, and check the import
# contracts in `.importlinter`.
lint:
    uv run ruff format --check src tests
    uv run ruff check src tests
    uv run lint-imports

# Rewrite what `lint` would complain about.
fmt:
    uv run ruff format src tests
    uv run ruff check --fix src tests

# Strict source and shared-helper typing. Test cases remain unannotated.
types:
    uv run mypy

# Lint the shell scripts that run as root.
shell-lint:
    uv run shellcheck install.sh \
        tests/container-install.sh tests/http3-edge-integration.sh

# Build the edge runtime image locally, the way CI and the integration test do.
#
# The tag is only a local name: production pulls the published image by the tag
# and digest recorded in the fleet settings, never a locally built one.
edge-image tag="blitzecdn-edge:dev":
    docker build --tag {{tag}} src/blitzecdn/assets/docker/edge

# Build the control-plane image from this checkout, the way the publishing
# workflow does and `BLITZECDN_CONTROL_PLANE_BUILD=1` makes install.sh do.
control-plane-image tag="blitzecdn-control-plane:dev":
    docker build --file src/blitzecdn/assets/docker/control-plane/Dockerfile --tag {{tag}} .

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
# `src/blitzecdn/assets/ansible/inventory/blitzecdn.yml`, where a missing database must stop a run.
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
        src/blitzecdn/assets/ansible/playbooks/edge.yml --syntax-check \
        --extra-vars @tests/fixtures/desired-state.yml \
        --extra-vars "$slots"
    uv run ansible-playbook \
        -i tests/fixtures/blitzecdn.yml \
        src/blitzecdn/assets/ansible/playbooks/decommission.yml --syntax-check \
        --extra-vars "$slots"
    uv run ansible-playbook \
        -i tests/fixtures/blitzecdn.yml \
        src/blitzecdn/capabilities/tls/ansible/playbooks/acme-challenge.yml \
        --syntax-check
    for play in \
        tests/integration/*.yml \
        src/blitzecdn/assets/ansible/playbooks/control-plane.yml \
        src/blitzecdn/assets/ansible/playbooks/uninstall.yml
    do
        uv run ansible-playbook -i localhost, "$play" --syntax-check
    done
    # Globs, not a list. Which plays and roles exist is a property of the
    # checkout, and a package that gains one should be linted because it is
    # there — not because somebody remembered to add a line here. The two
    # cache roles reached only by cache's own plays were already being linted
    # transitively; naming them by glob makes that explicit and costs nothing.
    ANSIBLE_INVENTORY=tests/fixtures/blitzecdn.yml uv run ansible-lint \
        src/blitzecdn/assets/ansible/playbooks/*.yml \
        src/blitzecdn/capabilities \
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
        src/blitzecdn/assets/docker/edge/Dockerfile
        src/blitzecdn/assets/docker/control-plane/Dockerfile
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
    uv run bandit -c pyproject.toml -r src
    uv run pip-audit

# Build the application wheel and source archive.
build:
    uv build

# The inner-loop gate: about a minute, and it catches everything that is
# cheap to catch. Formatting, types, the shell scripts, every YAML and role,
# and the whole suite across cores with coverage off.
#
# The full gate adds coverage, installed-wheel verification, audit and builds.
#
# Not a substitute for `check` before pushing. It is not what CI runs.
#
# `docker-lint` is left out for the same reason as the rest: without a local
# hadolint it pulls an image, and requiring a running Docker daemon for the
# inner loop would cost more than the gate catches on an edit that changes no
# Dockerfile. Run `just docker-lint` directly when one changes.
check-quick: lint types shell-lint ansible-check test-fast

# Everything CI runs. Run this before pushing.
check: lock-check lint types shell-lint test ansible-check docker-lint audit build

# --- published surfaces --------------------------------------------------

# Rewrite the frozen public surfaces under `tests/contract/frozen/`.
#
# Run this only when a change to a *public* surface is intended: the HTTP
# routes and schemas, the CLI commands, the Ansible roles and variables, or
# the database schema. Everything in those files is something outside this
# repository can bind to — an operator's inventory or an API client — and
# after the first release changing one is a version decision
# rather than a refresh.
#
# Deliberately not a `--update` flag on the tests: a flag is one keystroke away
# from whoever is trying to turn a red suite green, and the point of these files
# is that touching them is its own act, visible in its own diff.
#
# Every shipped surface has one owner, the application distribution.
refreeze:
    uv sync --frozen
    uv run python tests/refreeze.py
    @git diff --stat -- tests/contract/frozen || true

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

# Build and install the single wheel in a fresh environment outside the checkout.
test-installation *args:
    uv run pytest --no-cov tests/architecture/test_application_installation.py {{args}}
