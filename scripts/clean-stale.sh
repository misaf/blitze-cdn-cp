#!/usr/bin/env bash
# Remove stale Python bytecode caches, and the directories left holding nothing
# else, from the checkout's source trees.
#
#   scripts/clean-stale.sh [--dry-run] [--quiet]
#   scripts/clean-stale.sh --install-hooks | --uninstall-hooks
#
# A pull or branch switch that moves or deletes a module leaves its ignored
# `__pycache__` behind, so the old package directory stays on disk with
# nothing but bytecode in it, and the tree looks as if it still had that code.
#
# What is removed, and only inside SCOPES below:
#   - every `__pycache__` directory and every stray `*.pyc` / `*.pyo` file
#     that git does not track;
#   - a directory that is empty *because* of that removal, walking upwards
#     until a directory still holds something or a scope root is reached.
#
# What is never touched: tracked files; a directory that was already empty
# before this ran (it may be there on purpose); anything outside the scopes,
# which is where `.env`, `.venv`, `.state`, worktrees and the tool caches live;
# and any `.state`, `.venv` or nested git checkout found inside a scope.
#
# Deliberately not `git clean -fdX`: that deletes every ignored file in the
# checkout, operational state and local configuration included.
#
# --install-hooks writes post-merge and post-checkout hooks that run this
# quietly after a pull, merge or branch switch. It refuses to replace a hook it
# did not write. The hooks live in the repository's common git directory, so
# every worktree of this clone runs them.
set -euo pipefail

readonly SCOPES=(src tests scripts)
readonly HOOKS=(post-merge post-checkout)
readonly HOOK_MARKER="# blitzecdn: clean-stale hook"

usage() {
  printf 'usage: %s [--dry-run] [--quiet]\n       %s --install-hooks | --uninstall-hooks\n' \
    "$0" "$0" >&2
  exit 2
}

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "${project_dir}"

dry_run=0
quiet=0
action=clean
for arg in "$@"; do
  case ${arg} in
    --dry-run) dry_run=1 ;;
    --quiet) quiet=1 ;;
    --install-hooks) action=install ;;
    --uninstall-hooks) action=uninstall ;;
    *) usage ;;
  esac
done

say() { [[ ${quiet} -eq 1 ]] || printf '%s\n' "$*"; }

hooks_dir() {
  local common
  common=$(git rev-parse --git-common-dir)
  # Relative to the current directory when git answers that way.
  [[ ${common} == /* ]] || common="${project_dir}/${common}"
  printf '%s/hooks' "${common}"
}

install_hooks() {
  local dir hook path
  if [[ -n $(git config --get core.hooksPath || true) ]]; then
    echo "core.hooksPath is set; git will not run hooks from $(hooks_dir)." >&2
    echo "Call scripts/clean-stale.sh --quiet from your own post-merge and post-checkout hooks instead." >&2
    exit 1
  fi
  dir=$(hooks_dir)
  mkdir -p -- "${dir}"
  for hook in "${HOOKS[@]}"; do
    path="${dir}/${hook}"
    if [[ -e ${path} ]] && ! grep -qF -- "${HOOK_MARKER}" "${path}"; then
      echo "${path} exists and was not written by this script; leaving it alone." >&2
      echo "Add this line to it instead: scripts/clean-stale.sh --quiet || true" >&2
      exit 1
    fi
  done
  for hook in "${HOOKS[@]}"; do
    path="${dir}/${hook}"
    # The hook runs from the worktree that triggered it, and does nothing in a
    # checkout of a revision that predates this script.
    cat >"${path}" <<EOF
#!/bin/sh
${HOOK_MARKER}
# Remove with: scripts/clean-stale.sh --uninstall-hooks
script="\$(git rev-parse --show-toplevel)/scripts/clean-stale.sh"
[ -x "\${script}" ] || exit 0
"\${script}" --quiet || true
EOF
    chmod +x "${path}"
    say "installed ${path}"
  done
}

uninstall_hooks() {
  local dir hook path
  dir=$(hooks_dir)
  for hook in "${HOOKS[@]}"; do
    path="${dir}/${hook}"
    if [[ -e ${path} ]] && grep -qF -- "${HOOK_MARKER}" "${path}"; then
      rm -f -- "${path}"
      say "removed ${path}"
    fi
  done
}

tracked() { [[ -n $(git ls-files -- "$1") ]]; }

# Paths removed so far, or that a dry run would have removed. A dry run deletes
# nothing, so it consults this to see that a parent would be left empty.
gone=()

is_gone() {
  local prior
  for prior in "${gone[@]+"${gone[@]}"}"; do
    [[ ${prior} == "$1" ]] && return 0
  done
  return 1
}

# True when DIR holds nothing except what is already in `gone`.
left_empty() {
  local entry
  for entry in "$1"/* "$1"/.[!.]* "$1"/..?*; do
    [[ -e ${entry} || -L ${entry} ]] || continue
    is_gone "${entry}" || return 1
  done
  return 0
}

remove() {
  local path=$1 label=$2
  gone+=("${path}")
  if [[ ${dry_run} -eq 1 ]]; then
    say "would remove ${label}"
  else
    rm -rf -- "${path}"
    say "removed ${label}"
  fi
}

clean() {
  local scope path dir candidates=() scopes=()
  for scope in "${SCOPES[@]}"; do
    [[ -d ${scope} ]] && scopes+=("${scope}")
  done
  [[ ${#scopes[@]} -gt 0 ]] || return 0

  # Collected before anything is deleted, so the walk never meets a directory
  # removed underneath it.
  while IFS= read -r -d '' path; do
    candidates+=("${path}")
  done < <(
    find "${scopes[@]}" \
      \( -name .git -o -name .state -o -name .venv \) -prune -o \
      -type d -exec test -e '{}/.git' \; -prune -o \
      \( -type d -name __pycache__ -prune -print0 \) -o \
      \( -type f \( -name '*.pyc' -o -name '*.pyo' \) -print0 \)
  )

  for path in "${candidates[@]+"${candidates[@]}"}"; do
    if tracked "${path}"; then
      say "keeping tracked ${path}"
      continue
    fi
    remove "${path}" "${path}"
    # Then the directories this removal emptied, up to a scope root. One that
    # was already empty is never reached: the walk only starts from a parent
    # of something that was removed.
    dir=$(dirname -- "${path}")
    while [[ ${dir} == */* && -d ${dir} ]] && left_empty "${dir}"; do
      remove "${dir}" "empty ${dir}/"
      dir=$(dirname -- "${dir}")
    done
  done

  [[ ${#gone[@]} -gt 0 ]] || say "no stale Python caches under ${scopes[*]}"
}

case ${action} in
  install) install_hooks ;;
  uninstall) uninstall_hooks ;;
  clean) clean ;;
esac
