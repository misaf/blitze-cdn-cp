#!/usr/bin/env bash
# Install one published release, update it to another, and prove that the
# operator's credentials and database state survive the real lifecycle.
#
#   tests/container-upgrade.sh ubuntu:24.04 v2.0.0 v2.0.1
set -Eeuo pipefail

readonly IMAGE=${1:?usage: container-upgrade.sh IMAGE FROM_VERSION TO_VERSION}
readonly FROM_VERSION=${2:?usage: container-upgrade.sh IMAGE FROM_VERSION TO_VERSION}
readonly TO_VERSION=${3:?usage: container-upgrade.sh IMAGE FROM_VERSION TO_VERSION}
readonly ADMIN_CIDR=203.0.113.8/32
readonly ACME_EMAIL=ops@example.com
readonly REPOSITORY=https://github.com/misaf/blitze-cdn-cp.git

container="blitzecdn-upgrade-$(printf '%s' "${IMAGE}" | tr -c 'a-z0-9' '-')-$$"

cleanup() {
  docker rm -f "${container}" >/dev/null 2>&1 || true
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
  bash -c 'apt-get update -qq && apt-get install -y -qq systemd systemd-sysv git ca-certificates >/dev/null && exec /sbin/init' \
  >/dev/null

for _ in $(seq 60); do
  state=$(in_container 'systemctl is-system-running' 2>/dev/null || true)
  [[ ${state} == running || ${state} == degraded ]] && break
  sleep 2
done
[[ ${state} == running || ${state} == degraded ]] ||
  fail "systemd never came up in ${IMAGE} (last state: ${state:-unknown})"

say "Installing ${FROM_VERSION}"
in_container "git clone --branch ${FROM_VERSION} ${REPOSITORY} /opt/blitzecdn" ||
  fail "could not clone ${FROM_VERSION}"
in_container "cd /opt/blitzecdn && ./install.sh standalone --admin-cidr ${ADMIN_CIDR} --email ${ACME_EMAIL}" ||
  fail "installing ${FROM_VERSION} failed"
in_container 'blitzecdn domain add upgrade-test.example' ||
  fail "could not create pre-upgrade database state"

credential_before=$(in_container 'sha256sum /etc/blitzecdn/blitzecdn.env')

say "Updating to ${TO_VERSION}"
in_container "cd /opt/blitzecdn && ./install.sh update --version ${TO_VERSION} --yes" ||
  fail "updating to ${TO_VERSION} failed"

credential_after=$(in_container 'sha256sum /etc/blitzecdn/blitzecdn.env')
[[ ${credential_before} == "${credential_after}" ]] ||
  fail "the update rewrote the API credential"
in_container "blitzecdn --version | grep -Fq '${TO_VERSION#v}'" ||
  fail "the CLI does not report ${TO_VERSION}"
in_container 'blitzecdn domain list | grep -Fq upgrade-test.example' ||
  fail "the update lost database state"
in_container 'systemctl is-active --quiet blitzecdn-api.service' ||
  fail "the API is not running after update"
in_container 'blitzecdn doctor --json >/dev/null' ||
  fail "doctor failed after update"

say "Uninstalling the upgraded release"
in_container 'cd /opt/blitzecdn && ./install.sh --uninstall --yes' ||
  fail "uninstall failed after update"

printf '\nPASS: %s upgraded from %s to %s with credentials and state intact\n' \
  "${IMAGE}" "${FROM_VERSION}" "${TO_VERSION}"
