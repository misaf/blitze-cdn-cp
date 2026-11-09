#!/usr/bin/env bash
# Report which development tasks this machine can run, and how to fix the rest.
#
#   scripts/doctor.sh [TASK...]     TASK: local (default), docker-lint, integration, ci, all
#
# Every task is always reported, so a contributor sees at a glance what runs
# here and what has to run in GitHub Actions. Only the tasks named on the
# command line decide the exit status: a laptop without Docker is a supported
# setup, so missing Docker fails `doctor integration` and never `doctor local`.
#
# A prerequisite that cannot be confirmed is reported as missing. A check that
# passes because it could not look would make this script exactly the green
# light on a machine that checked nothing that it exists to prevent.
set -uo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "${project_dir}" || exit 2

readonly TASKS=(local docker-lint integration ci)

requested=("$@")
[[ ${#requested[@]} -gt 0 ]] || requested=(local)
for task in "${requested[@]}"; do
  case ${task} in
    all) requested=("${TASKS[@]}") && break ;;
    local | docker-lint | integration | ci) ;;
    *)
      printf 'usage: doctor.sh [%s|all]...\n' "$(IFS='|' && printf '%s' "${TASKS[*]}")" >&2
      exit 2
      ;;
  esac
done

# Per-task state: "ok" until a check of that task fails. Plain variables, not
# an associative array: macOS still ships bash 3.2, and a laptop is exactly
# where this runs.
status_var() { printf 'status_%s' "${1//-/_}"; }
set_status() { printf -v "$(status_var "$1")" '%s' "$2"; }
get_status() { local name; name=$(status_var "$1") && printf '%s' "${!name}"; }
for task in "${TASKS[@]}"; do set_status "${task}" ok; done

ok() { printf '  ok       %s\n' "$1"; }
missing() {
  local task=$1 what=$2 fix=$3
  set_status "${task}" missing
  printf '  MISSING  %s\n           fix: %s\n' "${what}" "${fix}"
}
have() { command -v "$1" >/dev/null 2>&1; }

# True when TOOL resolves inside the project environment, not elsewhere on PATH:
# `uv run --no-sync` falls back to PATH, which would credit a global install.
from_project() {
  uv run --no-sync python -c '
import shutil, sys
found = shutil.which(sys.argv[1])
sys.exit(not (found and found.startswith(sys.prefix)))' "$1" >/dev/null 2>&1
}

docker_running() { have docker && docker info >/dev/null 2>&1; }

# --- local: lint, typing, shell and Ansible checks, and the unit suite -------
printf 'local checks (just check-quick, just test, just ansible-check)\n'
if have git; then ok git; else missing local git 'install Git'; fi
if have just; then ok just; else missing local just 'https://just.systems/man/en/packages.html'; fi
if have uv; then
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
  if uv python find '>=3.15.2' >/dev/null 2>&1; then
    ok "Python $(uv run --no-sync python -c 'import platform; print(platform.python_version())' 2>/dev/null || uv python find '>=3.15.2')"
  else
    missing local 'Python 3.15.2 or later' 'uv python install 3.15.2'
  fi
  if uv sync --frozen --check >/dev/null 2>&1; then
    ok 'project environment matches uv.lock'
  else
    missing local 'project environment matches uv.lock' 'just install'
  fi
  # Ansible and ShellCheck come from the project environment, never a global
  # installation, so the versions here are the versions CI runs.
  if from_project ansible && version=$(uv run --no-sync ansible --version 2>/dev/null | head -n 1); then
    ok "${version} (project environment)"
  else
    missing local 'Ansible from the project environment' 'just install'
  fi
  if from_project shellcheck; then
    ok 'ShellCheck (project environment)'
  else
    missing local 'ShellCheck from the project environment' 'just install'
  fi
else
  missing local uv 'https://docs.astral.sh/uv/getting-started/installation/'
fi
# The pinned collections, at their pinned versions: a role that cannot resolve
# a module reads as a broken role rather than a missing dependency. The same
# script gates `just ansible-check`, so the two cannot disagree.
while read -r name version state; do
  if [[ ${state} == ok ]]; then
    ok "collection ${name} ${version}"
  else
    missing local "collection ${name} ${version}" 'just collections'
  fi
done < <(bash scripts/collections-check.sh)

# --- docker-lint: part of `just check`, not of `just check-quick` ------------
printf '\nDockerfile lint (just docker-lint; part of just check)\n'
if have hadolint; then
  ok "hadolint $(hadolint --version 2>/dev/null | awk '{print $NF}')"
elif docker_running; then
  ok 'Docker daemon (runs hadolint in a container)'
else
  missing docker-lint 'hadolint or a running Docker daemon' \
    'brew install hadolint, or start Docker; CI runs this in the quality job'
fi

# --- integration: the privileged container harnesses ------------------------
printf '\nContainer integration (tests/container-install.sh, just test-integration-http3)\n'
if docker_running; then
  ok "Docker daemon $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
  if [[ $(uname -s) == Linux ]]; then
    ok 'Linux host (privileged systemd containers)'
  else
    missing integration 'Linux host for privileged systemd containers' \
      'push the branch and use the install and http3-integration CI jobs'
  fi
else
  missing integration 'a running Docker daemon' \
    'push the branch and use the install and http3-integration CI jobs'
fi

# --- ci: triggering and inspecting GitHub Actions from here ------------------
printf '\nGitHub Actions from this machine (docs/testing.md)\n'
if have gh; then
  if gh auth status >/dev/null 2>&1; then
    ok 'gh, authenticated'
  else
    missing ci 'gh authenticated' 'gh auth login'
  fi
else
  missing ci 'gh (GitHub CLI)' 'https://cli.github.com/, or use the Actions tab'
fi

printf '\n'
result=0
for task in "${requested[@]}"; do
  printf '%-12s %s\n' "${task}" "$(get_status "${task}")"
  [[ $(get_status "${task}") == ok ]] || result=1
done
for task in "${TASKS[@]}"; do
  [[ " ${requested[*]} " == *" ${task} "* ]] && continue
  printf '%-12s %s (not required; pass "%s" to require it)\n' "${task}" "$(get_status "${task}")" "${task}"
done
exit "${result}"
