#!/usr/bin/env bash
# Copy what a failed integration run knows out of its throwaway host.
#
#   scripts/collect-diagnostics.sh OUTPUT_DIR HOST_CONTAINER [OTHER_CONTAINER...]
#
# The integration harnesses call this from their EXIT trap when they fail and
# DIAGNOSTICS_DIR is set, before the containers are removed. CI uploads the
# directory as an artifact, so a failure can be investigated from the run page
# instead of by reproducing a privileged, systemd-in-Docker setup on a laptop.
#
# HOST_CONTAINER is the systemd host the harness provisioned; its journal, its
# failed units and every container its own engine runs are collected. Other
# containers (an origin, a client) contribute their logs and state only.
#
# Best effort by design: each command that fails leaves its error in the file
# it would have written, and nothing here changes the harness's exit status.
# Configuration files are deliberately not copied: they hold the run's API
# credential, and an artifact is readable by anyone who can read the run.
set -uo pipefail

readonly out=${1:?usage: collect-diagnostics.sh OUTPUT_DIR HOST_CONTAINER [CONTAINER...]}
readonly host=${2:?usage: collect-diagnostics.sh OUTPUT_DIR HOST_CONTAINER [CONTAINER...]}
shift 2

mkdir -p -- "${out}" || exit 0

# capture FILE COMMAND...: the command's output and errors, bounded in time so a
# wedged engine cannot hold the job until its timeout. `timeout` is GNU
# coreutils, so a macOS run by hand goes without the bound.
bound=()
command -v timeout >/dev/null 2>&1 && bound=(timeout 60)
capture() {
  local file=$1
  shift
  ${bound[@]+"${bound[@]}"} "$@" >"${out}/${file}" 2>&1 ||
    printf '\n[exit status %s]\n' "$?" >>"${out}/${file}"
}
# An executable, not a function, so `timeout` can run it.
on_host=(docker exec "${host}" bash -c)

# capture_host FILE SCRIPT: SCRIPT run on the provisioned host.
capture_host() { capture "$1" "${on_host[@]}" "$2"; }

for container in "${host}" "$@"; do
  capture "${container}.inspect.json" docker inspect "${container}"
  capture "${container}.log" docker logs --timestamps --tail 2000 "${container}"
done

capture_host host-journal.log 'journalctl --no-pager --output=short-iso --lines=5000'
capture_host host-failed-units.txt 'systemctl --failed --no-pager; systemctl list-units "blitzecdn*" --all --no-pager'
capture_host host-containers.txt 'docker ps --all --no-trunc; echo; docker images'
capture_host host-listeners.txt 'ss -lntup'
capture_host host-disk.txt 'df -h; echo; du -sh /var/lib/docker 2>/dev/null'
capture_host host-blitzecdn-tree.txt 'ls -la --recursive /etc/blitzecdn /var/lib/blitzecdn 2>&1 | head -n 2000'

# Bounded like every capture: a wedged inner engine must cost this one command,
# not the job's remaining time and with it the upload of everything above.
names=$(${bound[@]+"${bound[@]}"} "${on_host[@]}" "docker ps --all --format '{{.Names}}'" 2>/dev/null || true)
for name in ${names}; do
  capture_host "inner-${name}.log" "docker logs --timestamps --tail 2000 ${name}"
  capture_host "inner-${name}.state.json" "docker inspect --format '{{json .State}}' ${name}"
done

printf 'Diagnostics written to %s\n' "${out}" >&2
exit 0
