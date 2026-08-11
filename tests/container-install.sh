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
# The working tree, not a clone: CI must test the commit under review. .venv is
# excluded because it holds binaries built for the host architecture.
tar --exclude=.venv --exclude=.state --exclude=.git \
  --exclude='*.pyc' --exclude=__pycache__ \
  -czf "${archive}" -C "${project_dir}" . 2>/dev/null
in_container 'mkdir -p /opt/blitzecdn'
# Not /tmp: systemd mounts a tmpfs over it on newer images, which hides
# anything docker cp put there first.
docker cp "${archive}" "${container}:/root/source.tgz" >/dev/null
in_container 'tar -xzf /root/source.tgz -C /opt/blitzecdn' 2>/dev/null

say "Installing"
in_container "cd /opt/blitzecdn && ./install.sh standalone --admin-cidr ${ADMIN_CIDR} --email ${ACME_EMAIL}" ||
  fail "install failed on ${IMAGE}"

say "Checking what the installation produced"
in_container 'getent passwd blitzecdn >/dev/null' || fail "no blitzecdn account"
in_container 'getent passwd deploy >/dev/null' || fail "no deploy account"
in_container 'test -x /usr/local/bin/blitzecdn' || fail "no CLI wrapper"
in_container 'test -f /etc/blitzecdn/blitzecdn.env' || fail "no environment file"
in_container 'systemctl is-active --quiet blitzecdn-api.service' || fail "API not running"
in_container 'systemctl is-enabled --quiet blitzecdn-drift.timer' || fail "drift timer not enabled"
# From / rather than the checkout: the wrapper exists to make that work.
in_container 'cd / && blitzecdn --version >/dev/null' || fail "CLI unusable outside the checkout"
in_container 'cd / && blitzecdn doctor --json >/dev/null' || fail "doctor failed"

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
