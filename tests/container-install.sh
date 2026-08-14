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
# It is also the only place the supported-platform claim is checked against a
# platform. Debian 12 is a supported edge but cannot run the control plane — it
# ships Python 3.11 — and that kind of gap is invisible to every other test.
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
in_container 'getent passwd blitzecdn >/dev/null' || fail "no blitzecdn account"
in_container 'getent passwd deploy >/dev/null' || fail "no deploy account"
in_container 'test -x /usr/local/bin/blitzecdn' || fail "no CLI wrapper"
in_container 'test -f /etc/blitzecdn/blitzecdn.env' || fail "no environment file"
in_container 'systemctl is-active --quiet blitzecdn-api.service' || {
  in_container 'systemctl status --no-pager blitzecdn-api.service' || true
  in_container 'journalctl --no-pager -u blitzecdn-api.service -n 100' || true
  fail "API not running"
}
in_container 'systemctl is-active --quiet redis-server.service' || fail "Redis not running"
in_container 'systemctl is-active --quiet blitzecdn-worker.service' || fail "worker not running"
# From / rather than the checkout: the wrapper exists to make that work.
in_container 'cd / && blitzecdn --version >/dev/null' || fail "CLI unusable outside the checkout"
in_container 'cd / && blitzecdn doctor --json >/dev/null' || fail "doctor failed"

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
in_container 'cd / && blitzecdn domain add example.test' || fail "could not add a zone"
in_container 'cd / && blitzecdn record add example.test cdn --value 127.0.0.1 --proxied' ||
  fail "could not add a proxied record"

# Check mode first: it must survive a host that has never converged, which is
# the case the `not ansible_check_mode` gates exist for.
in_container 'cd / && blitzecdn plan --json >/dev/null' || fail "check-mode run failed"

in_container 'cd / && blitzecdn deploy --yes --json >/dev/null' || fail "deploy failed"
in_container 'test -f /etc/nginx/sites-enabled/cdn-example-test.conf' ||
  fail "the deploy did not enable the managed site"
in_container 'nginx -t' || fail "the converged nginx configuration does not load"
in_container 'grep -q "^cdn-example-test$" /etc/nginx/blitzecdn-managed-sites' ||
  fail "the managed-site registry was not written"
in_container 'systemctl is-active --quiet nginx' || fail "nginx is not running"

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
in_container 'nginx -t' || fail "nginx does not load after the site was withdrawn"

say "Backing up the database while the API is serving"
in_container 'cd / && blitzecdn db backup /var/lib/blitzecdn/backup.db' ||
  fail "db backup failed"
in_container 'test -s /var/lib/blitzecdn/backup.db' || fail "the backup is empty"

say "Re-running the installer"
credential_before=$(in_container 'sha256sum /etc/blitzecdn/blitzecdn.env')
in_container "cd /opt/blitzecdn && ./install.sh standalone --admin-cidr ${ADMIN_CIDR} --email ${ACME_EMAIL}" ||
  fail "the installer is not re-runnable on ${IMAGE}"
credential_after=$(in_container 'sha256sum /etc/blitzecdn/blitzecdn.env')
# The invariant that matters most: re-running must never rotate the API
# credential an operator is already using.
[[ ${credential_before} == "${credential_after}" ]] ||
  fail "re-running the installer rewrote the API credential"

say "Uninstalling"
in_container 'cd /opt/blitzecdn && ./install.sh --uninstall --yes' ||
  fail "uninstall failed on ${IMAGE}"

say "Checking the host is clean"
for path in /opt/blitzecdn /etc/blitzecdn /usr/local/bin/blitzecdn \
  /etc/sudoers.d/blitzecdn-deploy /var/lib/blitzecdn /home/deploy; do
  in_container "test ! -e ${path}" || fail "${path} survived the uninstall"
done
in_container 'getent passwd blitzecdn >/dev/null' && fail "blitzecdn account survived"
in_container 'getent passwd deploy >/dev/null' && fail "deploy account survived"
in_container 'ls /etc/systemd/system | grep -q blitzecdn' && fail "unit files survived"

printf '\nPASS: %s completed install, re-install, and uninstall\n' "${IMAGE}"
