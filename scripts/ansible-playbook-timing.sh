#!/usr/bin/env bash
# Stand-in for `ansible-playbook` that times every invocation a rollout makes.
#
# For benchmarking on disposable edges only (docs/design/rollout-benchmark.md).
# Install it as `ansible-playbook` in a directory put first on the controller's
# PATH, then run a deploy or drift check as usual. Each invocation appends one
# tab-separated line to BLITZE_BENCH_LOG:
#
#   start_epoch  wall_s  user_s  sys_s  max_rss_kib  rc  check  tags  limit
#
# and `scripts/rollout_benchmark.py --summarize BLITZE_BENCH_LOG` reads it back.
# Needs GNU time at /usr/bin/time (the `time` package on Debian and Ubuntu).
set -euo pipefail

log="${BLITZE_BENCH_LOG:?set BLITZE_BENCH_LOG to the file to append timings to}"
real="${BLITZE_BENCH_ANSIBLE_PLAYBOOK:?set BLITZE_BENCH_ANSIBLE_PLAYBOOK to the real ansible-playbook}"

check=0 tags="" limit=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[i]}" in
    --check) check=1 ;;
    --tags) tags="${args[i + 1]:-}" ;;
    --tags=*) tags="${args[i]#--tags=}" ;;
    --limit | -l) limit="${args[i + 1]:-}" ;;
    --limit=*) limit="${args[i]#--limit=}" ;;
  esac
done

usage="$(mktemp)"
trap 'rm -f "${usage}"' EXIT
start="$(date +%s.%N)"
rc=0
/usr/bin/time -o "${usage}" -f '%e\t%U\t%S\t%M' "${real}" "$@" || rc=$?
printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
  "${start}" "$(tail -n 1 "${usage}")" "${rc}" "${check}" "${tags:--}" "${limit:--}" \
  >>"${log}"
exit "${rc}"
