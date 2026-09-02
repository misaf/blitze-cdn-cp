#!/usr/bin/env bash
# Run the whole installer lifecycle against a throwaway container.
#
#   tests/container-install.sh debian:13
#
# This is the only test that exercises what `install.sh standalone` actually
# does. Everything else asserts on the script's shape or runs it in a sandbox
# with the privileged commands stubbed, because provisioning needs root and a
# real init. Here it gets both: systemd as PID 1, a real apt, real accounts.
#
# It also checks the supported fresh-edge platform contract against a real OS.
#
# Requires Docker and about five minutes per image.
set -Eeuo pipefail

readonly IMAGE=${1:?usage: container-install.sh IMAGE}
readonly ADMIN_CIDR=203.0.113.8/32
readonly ACME_EMAIL=ops@example.com

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
container="blitzecdn-$(printf '%s' "${IMAGE}" | tr -c 'a-z0-9' '-')-$$"
archive=$(mktemp -t blitzecdn-source-XXXXXX).tgz

cleanup() {
  docker rm -f "${container}" >/dev/null 2>&1 || true
  rm -f -- "${archive}"
}
trap cleanup EXIT

say() { printf '\n=== %s ===\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

in_container() { docker exec "${container}" bash -c "$1"; }
# Same, but with stdin attached, for streaming an image into the host's engine.
into_container() { docker exec -i "${container}" bash -c "$1"; }

say "Starting ${IMAGE} with systemd"
docker run -d --name "${container}" \
  --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --tmpfs /run --tmpfs /run/lock \
  "${IMAGE}" \
  bash -c 'apt-get update -qq && apt-get install -y -qq systemd systemd-sysv >/dev/null && exec /sbin/init' \
  >/dev/null

# systemd needs a moment, and how long depends on the image and the runner.
# Poll rather than sleep so a fast host is not punished and a slow one is not
# flaky. "degraded" is accepted: units unrelated to BlitzeCDN routinely fail in
# a container, and refusing to continue would test Docker rather than the
# installer.
for _ in $(seq 60); do
  state=$(in_container 'systemctl is-system-running' 2>/dev/null || true)
  [[ ${state} == running || ${state} == degraded ]] && break
  sleep 2
done
[[ ${state} == running || ${state} == degraded ]] ||
  fail "systemd never came up in ${IMAGE} (last state: ${state:-unknown})"

say "Copying the working tree to /opt/blitzecdn"
# The working tree, not a clone: CI must test the commit under review. Runtime
# state, caches, local configuration and host metadata must not cross this
# boundary. In particular, macOS tar otherwise emits AppleDouble `._*.py`
# sidecars for extended attributes; Alembic treats those binary files as
# revisions and a clean Linux installation fails with a null-byte SyntaxError.
COPYFILE_DISABLE=1 tar \
  --exclude=.venv --exclude='.venv.invalid.*' \
  --exclude=.state --exclude=.git --exclude=.ansible --exclude=.cache \
  --exclude=.env --exclude=blitzecdn.toml --exclude=.codex \
  --exclude=dist --exclude='*.egg-info' \
  --exclude=.mypy_cache --exclude=.pytest_cache --exclude=.ruff_cache \
  --exclude=.hypothesis --exclude=.coverage --exclude=coverage.xml \
  --exclude=htmlcov --exclude=.DS_Store --exclude='._*' \
  --exclude='*.pyc' --exclude=__pycache__ \
  -czf "${archive}" -C "${project_dir}" . 2>/dev/null
in_container 'mkdir -p /opt/blitzecdn'
# Not /tmp: systemd mounts a tmpfs over it on newer images, which hides
# anything docker cp put there first.
docker cp "${archive}" "${container}:/root/source.tgz" >/dev/null
in_container 'tar -xzf /root/source.tgz -C /opt/blitzecdn' 2>/dev/null

say "Installing"
if ! in_container "cd /opt/blitzecdn && ./install.sh standalone --admin-cidr ${ADMIN_CIDR} --email ${ACME_EMAIL}"; then
  # Preserve the evidence before the EXIT trap removes the disposable host.
  # This catches corrupt source/package copies that otherwise surface only as
  # an opaque import error several layers inside the installer.
  # The expansions belong to the container shell.
  # shellcheck disable=SC2016
  in_container 'for path in /opt/blitzecdn/src/blitzecdn/migrations/versions/0001_initial_schema.py /opt/blitzecdn/.venv/lib/python3.13/site-packages/blitzecdn/migrations/versions/0001_initial_schema.py; do printf "%s bytes=" "$path"; wc -c < "$path"; done' || true
  in_container 'sha256sum /opt/blitzecdn/src/blitzecdn/migrations/versions/0001_initial_schema.py /opt/blitzecdn/.venv/lib/python3.13/site-packages/blitzecdn/migrations/versions/0001_initial_schema.py' || true
  fail "install failed on ${IMAGE}"
fi

say "Checking what the installation produced"
in_container '! getent passwd blitzecdn >/dev/null' || fail "unexpected blitzecdn host account"
in_container '! getent group blitzecdn >/dev/null' || fail "unexpected blitzecdn host group"
in_container 'getent passwd deploy >/dev/null' || fail "no deploy account"
in_container 'test -x /usr/local/bin/blitzecdn' || fail "no CLI wrapper"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-api | grep -qx healthy' || {
  in_container 'docker compose --file /etc/blitzecdn/control-plane.compose.yml ps' || true
  in_container 'docker compose --file /etc/blitzecdn/control-plane.compose.yml logs blitzecdn-api' || true
  fail "API not running"
}
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-redis | grep -qx healthy' || fail "Redis not running"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-worker | grep -qx healthy' || fail "worker not running"
for service in blitzecdn-api blitzecdn-worker; do
  in_container "docker inspect -f '{{.Config.User}}' ${service} | grep -qx nobody:nogroup" ||
    fail "${service} does not declare the reused image identity"
  in_container "docker exec ${service} id | grep -q 'uid=65534(nobody) gid=65534(nogroup)'" ||
    fail "${service} is not running as nobody:nogroup"
done
in_container 'docker compose --file /etc/blitzecdn/control-plane.compose.yml run --rm --no-deps --entrypoint id blitzecdn-cli | grep -q "uid=65534(nobody) gid=65534(nogroup)"' ||
  fail "CLI is not running as nobody:nogroup"
in_container 'stat -c %u:%g:%a /var/lib/blitzecdn | grep -qx 65534:65534:700' ||
  fail "persistent state ownership does not match the container identity"
in_container 'stat -c %u:%g:%a /var/backups/blitzecdn | grep -qx 65534:65534:700' ||
  fail "backup ownership does not match the container identity"
in_container 'stat -c %U:%G:%a /etc/blitzecdn/blitzecdn.env | grep -qx root:root:600' ||
  fail "service secrets are not restricted to host root"
in_container 'stat -c %U:%G:%a /opt/blitzecdn/blitzecdn.toml | grep -qx root:root:644' ||
  fail "non-secret configuration is not host-managed"
in_container 'docker exec blitzecdn-api python -c "import sqlite3; p=\"/opt/blitzecdn/.state/permission-test.db\"; c=sqlite3.connect(p); assert c.execute(\"PRAGMA journal_mode=WAL\").fetchone()[0] == \"wal\"; c.execute(\"CREATE TABLE writable (id INTEGER)\"); c.commit(); c.close()"' ||
  fail "SQLite could not create and write WAL state as the application identity"
in_container 'test -f /var/lib/blitzecdn/permission-test.db && rm -f /var/lib/blitzecdn/permission-test.db*' ||
  fail "SQLite permission test did not write into persistent state"
# From / rather than the checkout: the wrapper exists to make that work.
in_container 'cd / && blitzecdn --version >/dev/null' || fail "CLI unusable outside the checkout"
in_container 'cd / && blitzecdn doctor --json >/dev/null' || fail "doctor failed"

say "Seeding the edge runtime image this host will run"
# The edge is a container now, so converging one needs an image before the
# first deploy — and CI must test the commit under review rather than a
# published image that lags it by definition. So: build it here, and load it
# into the disposable host's own engine.
#
# Which needs an engine, before the converge that installs one. The repository
# is therefore seeded exactly as blitzecdn_docker seeds it, into the same
# deb822 file the role writes, so the converge that follows still finds
# everything as it expects and reports no change on its second run. This is the
# only step in this script that pre-empts a role, and it does so with that
# role's own configuration.
docker build --quiet --tag blitzecdn-edge:standalone "${project_dir}/src/blitzecdn/docker/edge" >/dev/null

in_container 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl gnupg >/dev/null' ||
  fail "could not install the Docker repository prerequisites"
in_container 'install -d -m 0755 /etc/apt/keyrings && curl -fsSL --proto "=https" --tlsv1.2 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc && chmod 0644 /etc/apt/keyrings/docker.asc' ||
  fail "could not fetch the Docker signing key"
# shellcheck disable=SC2016
in_container 'printf "Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n" "$(. /etc/os-release && printf %s "${VERSION_CODENAME}")" "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/docker.sources' ||
  fail "could not configure the Docker repository"
in_container 'DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null' ||
  fail "could not install Docker Engine"
in_container 'systemctl enable --now docker' || fail "Docker did not start"
docker save blitzecdn-edge:standalone | into_container 'docker load' >/dev/null ||
  fail "could not load the edge runtime image into the disposable host"

say "Converging this host as an edge"
# The one place an edge role is ever executed. Everything else about the roles
# is checked by shape — ansible-lint, --syntax-check, the argument-spec
# contract test — and none of that runs a task. So the nginx block/rescue, the
# managed-site registry that drives stale removal, the `not ansible_check_mode`
# gates and the package retries were all unexercised: a role could be rewritten
# into something that cannot converge and every gate would stay green.
#
# The post-bootstrap CLI handoff records edge-local; this verifies that handoff
# rather than creating a second inventory row for the same machine.
in_container 'cd / && blitzecdn edge list --json | grep -q '"'"'"name": "edge-local"'"'"'' ||
  fail "the installer did not register the local edge"
# Fleet policy, set the way an operator sets it: the image an edge runs is a
# database-backed setting the inventory plugin publishes, not desired state.
in_container 'cd / && blitzecdn config set blitzecdn_edge_image blitzecdn-edge:standalone' ||
  fail "could not pin the edge runtime image"
in_container 'cd / && blitzecdn config set blitzecdn_edge_stack_image_pull false' ||
  fail "could not disable the registry pull"

in_container 'cd / && blitzecdn domain add example.test' || fail "could not add a zone"
in_container 'cd / && blitzecdn record add example.test cdn --value 127.0.0.1 --proxied' ||
  fail "could not add a proxied record"

# Check mode first: it must survive a host that has never converged, which is
# the case the `not ansible_check_mode` gates exist for.
in_container 'cd / && blitzecdn plan --json >/dev/null' || {
  # shellcheck disable=SC2016
  in_container 'latest=$(ls -t /opt/blitzecdn/.state/logs/*.log | head -1); printf "Ansible log: %s\n" "$latest"; sed -n "1,240p" "$latest"' || true
  fail "check-mode run failed"
}

in_container 'cd / && blitzecdn deploy --yes --json >/dev/null' || fail "deploy failed"
in_container 'test -f /etc/nginx/sites-enabled/cdn-example-test.conf' ||
  fail "the deploy did not enable the managed site"
in_container 'docker exec blitzecdn-edge nginx -t' ||
  fail "the converged nginx configuration does not load"
in_container 'grep -q "^cdn-example-test$" /etc/nginx/blitzecdn-managed-sites' ||
  fail "the managed-site registry was not written"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-edge | grep -qx healthy' ||
  fail "the edge container is not healthy"
# The host must be left with no traffic-serving BlitzeCDN runtime packages of
# its own: a host process would compete with the container for public ports.
in_container '! command -v nginx' || fail "nginx was installed on the host"

# Converging twice must change nothing: the drift check is the assertion.
in_container 'cd / && blitzecdn deploy --yes --json >/dev/null' || fail "second deploy failed"
in_container 'cd / && blitzecdn drift --json' || {
  # shellcheck disable=SC2016
  in_container 'latest=$(ls -t /opt/blitzecdn/.state/logs/*.log | head -1); printf "Ansible log: %s\n" "$latest"; sed -n "1,240p" "$latest"' || true
  fail "the fleet reports drift immediately after converging"
}

# Removing the record must withdraw the vhost, which is the registry's job.
in_container 'cd / && blitzecdn record remove example.test cdn --yes' ||
  fail "could not remove the record"
in_container 'cd / && BLITZE_ALLOW_EMPTY_SITES=true blitzecdn deploy --yes --json >/dev/null' ||
  fail "withdrawing the last site failed"
in_container 'test ! -e /etc/nginx/sites-enabled/cdn-example-test.conf' ||
  fail "the stale site was left enabled"
in_container 'docker exec blitzecdn-edge nginx -t' ||
  fail "nginx does not load after the site was withdrawn"

say "Backing up the control plane while the API is serving"
in_container 'cd / && blitzecdn backup create' ||
  fail "backup create failed"
in_container 'ls /var/backups/blitzecdn/blitzecdn-backup-*Z.tar.gz >/dev/null' ||
  fail "no backup landed in /var/backups/blitzecdn"
# The expansions belong to the container shell: the archive is named for the
# moment it was taken, so the test cannot know its name in advance.
# shellcheck disable=SC2016
in_container 'test "$(stat -c %a "$(ls -t /var/backups/blitzecdn/*.tar.gz | head -1)")" = 600' ||
  fail "the backup archive is not 0600"
# shellcheck disable=SC2016
in_container 'cd / && blitzecdn backup inspect "$(ls -t /var/backups/blitzecdn/*.tar.gz | head -1)" | grep -q "^  database$"' ||
  fail "the backup does not declare a database component"
in_container 'cd / && blitzecdn backup create --only database -o /opt/blitzecdn/.state/database-only.tar.gz' ||
  fail "a database-only backup failed"
in_container 'test -s /var/lib/blitzecdn/database-only.tar.gz' || fail "the backup is empty"
in_container 'cd / && blitzecdn backup restore /var/lib/blitzecdn/database-only.tar.gz --yes' ||
  fail "a database-only restore failed"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-api | grep -qx healthy' ||
  fail "the database restore did not restart the API"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-worker | grep -qx healthy' ||
  fail "the database restore did not restart the worker"

say "Re-running the installer"
in_container "cd /opt/blitzecdn && ./install.sh standalone --admin-cidr ${ADMIN_CIDR} --email ${ACME_EMAIL}" ||
  fail "the installer is not re-runnable on ${IMAGE}"

say "Checking the updater refuses a checkout it cannot verify"
# The working tree is copied in here rather than cloned, so this host has no
# .git and `update` must refuse rather than run against an unknown source. The
# happy path needs a real clone and a network fetch, which would test origin
# rather than the commit under review; what this pins is that the refusal is
# clean — a host that was serving before a refused update is still serving
# after it, because nothing was stopped on the way to the refusal.
in_container 'cd /opt/blitzecdn && ./install.sh update --yes' &&
  fail "update ran against a checkout with no origin"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-api | grep -qx healthy' ||
  fail "a refused update stopped the API"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-worker | grep -qx healthy' ||
  fail "a refused update stopped the worker"

say "Uninstalling"
in_container 'cd /opt/blitzecdn && ./install.sh --uninstall --yes' ||
  fail "uninstall failed on ${IMAGE}"

say "Checking the host is clean"
for path in /opt/blitzecdn /etc/blitzecdn /usr/local/bin/blitzecdn \
  /etc/sudoers.d/blitzecdn-deploy /var/lib/blitzecdn /home/deploy; do
  in_container "test ! -e ${path}" || fail "${path} survived the uninstall"
done
in_container 'getent passwd blitzecdn >/dev/null' && fail "unexpected blitzecdn account appeared"
in_container 'getent group blitzecdn >/dev/null' && fail "unexpected blitzecdn group appeared"
in_container 'getent passwd deploy >/dev/null' && fail "deploy account survived"
in_container 'ls /etc/systemd/system | grep -q blitzecdn' && fail "unit files survived"
in_container 'docker ps --all --format "{{.Names}}" | grep -q "^blitzecdn-\(api\|worker\|redis\)$"' &&
  fail "control-plane containers survived"
in_container 'docker volume inspect blitzecdn-redis >/dev/null 2>&1' &&
  fail "control-plane Redis volume survived"

printf '\nPASS: %s completed install, re-install, and uninstall\n' "${IMAGE}"
