#!/usr/bin/env bash
# BlitzeCDN installer.
#
# Four jobs live here behind subcommands:
#
#   (no arguments)  build the virtualenv and install the pinned collections
#   standalone      provision this whole server as control plane and edge
#   update          move an installed host onto a newer release
#
# The no-argument form installs the current checkout for controller-only use.
set -Eeuo pipefail

readonly INSTALL_DIR=/opt/blitzecdn
readonly CONFIG_DIR=/etc/blitzecdn
readonly CLI_WRAPPER=/usr/local/bin/blitzecdn
readonly CONTROL_PLANE_COMPOSE_FILE=/etc/blitzecdn/control-plane.compose.yml
readonly CONTROL_PLANE_SERVICES=(blitzecdn-api blitzecdn-worker)
# The optional distributions a controller is attached to when nothing says
# otherwise. Spelled once: it drives both the `uv sync --extra` flags and the
# capability list the control-plane role is given, and those two disagreeing
# would write one capability's configuration onto a controller that does not
# have it — which the control plane refuses to start on, by design.
readonly DEFAULT_CAPABILITIES="backup cache hardening origins resolver"

# The capabilities this controller installs, and the two shapes they are needed
# in. One reader, two projections: the list was expanded by hand in both places
# before, which is exactly the disagreement the comment above warns about.
capabilities() {
  printf '%s\n' "${BLITZECDN_CAPABILITIES:-${DEFAULT_CAPABILITIES}}"
}

# One `--extra NAME` pair per capability, one per line for the caller to read
# into an array.
capability_extras() {
  local capability
  for capability in $(capabilities); do
    printf -- '--extra\n%s\n' "${capability}"
  done
}

# The same list as a JSON array body, for --extra-vars.
capability_json() {
  local capability separator='' rendered=''
  for capability in $(capabilities); do
    rendered+="${separator}\"${capability}\""
    separator=,
  done
  printf '%s\n' "${rendered}"
}

# Captured before dispatch strips the subcommand so destructive modes can
# re-exec a private copy after deleting the checkout that contained this file.
original_args=("$@")
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

usage_root() {
  cat <<'EOF'
Usage: ./install.sh [SUBCOMMAND] [OPTIONS]

Install BlitzeCDN.

Subcommands:
  (none)      Build .venv and install the pinned Ansible collections
  standalone  Provision this server as an independent control plane and edge
  update      Move an installed server onto a newer release, keeping its state
  help        Show this message

Whole-host operations:
  --uninstall Remove every BlitzeCDN artifact and stop there
  --fresh     Uninstall, then reinstall like a brand-new server

Run './install.sh SUBCOMMAND --help' for the options of a subcommand.

Environment for the no-argument form:
  BLITZECDN_PYTHON     Python interpreter to build .venv with (default python3)
  BLITZECDN_DEV=1      Install -e '.[dev]' instead of a plain wheel
  BLITZECDN_WRAPPER_DIR
                       Where to put the `blitzecdn` command (default ~/.local/bin)
  BLITZECDN_USER_WRAPPER=0
                       Do not put `blitzecdn` on PATH; use .venv/bin/blitzecdn

The no-argument form installs a `blitzecdn` command that pins this checkout as
the project directory, so it can be run from any working directory.
EOF
}

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Fail with one or more stderr lines. The first argument is the exit status,
# because the CLI's documented codes matter here: 2 is invalid input and 1 is a
# host or environment that cannot be installed onto.
die() {
  local status=$1
  shift
  printf '%s\n' "$@" >&2
  exit "${status}"
}

# Temporary paths this run is responsible for, removed however it ends.
#
# Registering a path is what makes the many `die` paths below safe: each one
# used to have to remember its own `rm`, and any exit that nobody had thought
# of — a `set -e` abort between a clone and the rename that consumes it — left
# the work behind. The emptiness guard is for Bash 3.2, where expanding an
# empty array under `set -u` is an error rather than nothing.
cleanup_paths=()
on_exit() {
  [[ ${#cleanup_paths[@]} -eq 0 ]] || rm -rf -- "${cleanup_paths[@]}"
}
trap on_exit EXIT

# mktemp, with the result registered for cleanup. Takes mktemp's arguments.
#
# Only for callers that run in this shell. A function whose output is taken
# with `$(...)` runs in a subshell, and neither the array push nor this trap
# survives it — such a caller has to clean up after itself, which is why
# `ensure_uv` still does.
temp_path() {
  local path
  path=$(mktemp "$@")
  cleanup_paths+=("${path}")
  printf '%s\n' "${path}"
}

# Stop treating a path as this run's to delete.
#
# For the moment a temporary thing becomes the real one. `--fresh` stages a
# clone that is disposable right up until teardown succeeds; past that point it
# is the only copy of the release on the host, and the recovery advice for a
# failed rename depends on it still being there.
unregister_path() {
  local keep=$1 path remaining=()
  for path in ${cleanup_paths[@]+"${cleanup_paths[@]}"}; do
    [[ ${path} == "${keep}" ]] || remaining+=("${path}")
  done
  cleanup_paths=(${remaining[@]+"${remaining[@]}"})
}

# The uv release this installer pins, and the checksum of each build of it.
#
# Pinned rather than "latest", and verified against a hash that lives in this
# repository rather than one fetched beside the download. A checksum served
# from the same place as the artifact only proves the bytes arrived intact; it
# says nothing about whether the artifact is the one this release was tested
# against. Refresh both together with `just uv-pin VERSION`.
#
# One pin per release target. The Linux builds are what a server installs; the
# macOS builds exist because a controller-only checkout is a developer machine
# as often as a server, and a Linux binary unpacked onto a Mac fails with
# "cannot execute binary file" long after the checksum said the bytes were fine.
UV_VERSION="0.12.3"
# shellcheck disable=SC2034 # read by indirect expansion in ensure_uv
UV_SHA256_x86_64_unknown_linux_gnu="600cf9a742aca00d292673b16b5acffaa7b8c269a364ad0c2e79498dcb1fe101"
# shellcheck disable=SC2034 # read by indirect expansion in ensure_uv
UV_SHA256_aarch64_unknown_linux_gnu="bb66cb52e7b1823aed1183630d8d8e5c958840d584a4c55ec10a4cfc168dcca2"
# shellcheck disable=SC2034 # read by indirect expansion in ensure_uv
UV_SHA256_x86_64_apple_darwin="4c9f52262a14da336e4a42ed24992d12d0c956acde87619e4611d321dffa602b"
# shellcheck disable=SC2034 # read by indirect expansion in ensure_uv
UV_SHA256_aarch64_apple_darwin="546f7f8a6c70ff13a3a9d2bc958db3427298cebf3e0cb756f9177133b7068843"

# Print the SHA-256 of a file.
#
# `sha256sum` is coreutils and ships on every server this installs onto; macOS
# has `shasum` instead and only grows `sha256sum` if somebody installed GNU
# coreutils. Checking both means the verification below cannot be skipped for
# want of a tool name.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    die 1 "error: need sha256sum or shasum to verify the uv download"
  fi
}

# Print the path to a usable uv, downloading the pinned build if necessary.
#
# An operator's own uv is used when it is at least the pinned version: a server
# that already manages uv through its package manager should not grow a second
# copy. Anything older, or nothing at all, gets the pinned build unpacked into
# .state/bin — inside the installation rather than on the system, so uninstall
# removes it and no other software on the host is affected by our choice.
ensure_uv() {
  local existing
  if existing="$(command -v uv 2>/dev/null)" && [[ -x "${existing}" ]]; then
    local have
    have="$("${existing}" --version 2>/dev/null | awk '{print $2}')"
    if [[ -n "${have}" ]] &&
      [[ "$(printf '%s\n%s\n' "${UV_VERSION}" "${have}" | sort -V | head -1)" == "${UV_VERSION}" ]]; then
      printf '%s\n' "${existing}"
      return 0
    fi
  fi

  local vendored=".state/bin/uv"
  if [[ -x "${vendored}" ]] &&
    [[ "$("${vendored}" --version 2>/dev/null | awk '{print $2}')" == "${UV_VERSION}" ]]; then
    printf '%s\n' "${PWD}/${vendored}"
    return 0
  fi

  # Both halves of the target triple are detected. Deriving only the
  # architecture and assuming Linux is what put a Linux binary on a Mac.
  local system machine platform architecture target pin expected
  system="$(uname -s)"
  machine="$(uname -m)"
  case "${system}" in
    Linux) platform="unknown-linux-gnu" ;;
    Darwin) platform="apple-darwin" ;;
    *)
      die 1 "error: no pinned uv build for ${system}; install uv ${UV_VERSION} or newer and rerun"
      ;;
  esac
  case "${machine}" in
    x86_64 | amd64) architecture="x86_64" ;;
    aarch64 | arm64) architecture="aarch64" ;;
    *)
      die 1 "error: no pinned uv build for ${machine}; install uv ${UV_VERSION} or newer and rerun"
      ;;
  esac
  target="${architecture}-${platform}"
  pin="UV_SHA256_${target//-/_}"
  expected="${!pin-}"
  [[ -n "${expected}" ]] ||
    die 1 "error: no pinned checksum for ${target}; install uv ${UV_VERSION} or newer and rerun"

  command -v curl >/dev/null 2>&1 ||
    die 1 "error: curl is required to download uv; install it and rerun"

  # Its own cleanup, not the shared stack: this function's output is read with
  # `$(...)`, so it runs in a subshell that the stack cannot reach.
  local archive
  archive="$(mktemp)"

  # stderr, not stdout: this function returns the uv path on stdout, and a
  # progress line there ends up substituted into the caller's variable.
  echo "Downloading uv ${UV_VERSION} (${target})..." >&2
  # --retry covers the 5xx and connection resets the release CDN serves now and
  # then; without it a single bad second fails a whole provisioning run. The
  # checksum below is what makes a retried download safe to trust.
  if ! curl -fsSL --proto '=https' --tlsv1.2 \
    --retry 3 --retry-connrefused --retry-all-errors --retry-delay 2 \
    -o "${archive}" \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${target}.tar.gz"; then
    rm -f -- "${archive}"
    die 1 "error: could not download uv ${UV_VERSION}"
  fi

  local actual
  actual="$(sha256_of "${archive}")"
  if [[ "${actual}" != "${expected}" ]]; then
    rm -f -- "${archive}"
    die 1 \
      "error: the uv download does not match its pinned checksum" \
      "       expected ${expected}" \
      "       received ${actual}" \
      "       Refusing to run it. Retry, and if it persists, do not proceed."
  fi

  mkdir -p .state/bin
  if ! tar -xzf "${archive}" -C .state/bin --strip-components=1 \
    "uv-${target}/uv"; then
    rm -f -- "${archive}"
    die 1 "error: could not unpack uv ${UV_VERSION}"
  fi
  rm -f -- "${archive}"
  chmod 0755 "${vendored}"
  printf '%s\n' "${PWD}/${vendored}"
}

# Guard a privileged subcommand. Every caller reports the same thing, so the
# EUID test exists once — the test harness neutralises this single line to
# exercise the destructive paths as an unprivileged user.
require_root() {
  [[ ${EUID} -eq 0 ]] || die 1 "error: run this ${1:-installer} with sudo"
}

# Every command uses this one parser. Parsed values are globals because Bash
# 3.2 (still shipped by macOS) has neither associative arrays nor namerefs.
parsed_yes=0
parsed_admin_cidr=''
parsed_email=''
parsed_deploy=0
parsed_allow_empty_sites=0
parsed_no_backup=0
parsed_public_addresses=()
parsed_forward_args=()

# Every subcommand, in one table: name, its usage function, what to call it in
# the sudo error (empty when it does not need root), and the options it takes.
#
# These four facts used to live in four hand-maintained lists — an
# `option_allowed` case, the two dispatch cases, and a `require_root` call in
# each cmd_* — so adding a subcommand meant editing all four and noticing if
# you missed one. A row is the whole declaration now.
#
# A string rather than an associative array on purpose: macOS still ships Bash
# 3.2, which has neither associative arrays nor namerefs, and the default
# install form runs on developer machines.
readonly COMMAND_TABLE='
install|usage_root||
standalone|usage_standalone|installer|--admin-cidr --email --deploy --allow-empty-sites --public-address
update|usage_update|updater|--yes --no-backup
uninstall|usage_uninstall|uninstaller|--yes
fresh|usage_fresh|installer|--admin-cidr --email --deploy --allow-empty-sites --public-address --yes
'

# Print one field of one command's row. Fields: 2 usage, 3 root label, 4 options.
command_field() {
  local name=$1 field=$2 row
  while IFS= read -r row; do
    [[ ${row%%|*} == "${name}" ]] || continue
    printf '%s\n' "${row}" | cut -d'|' -f"${field}"
    return 0
  done <<EOF
${COMMAND_TABLE}
EOF
  return 1
}

option_allowed() {
  local command=$1 option=$2 allowed
  allowed=" $(command_field "${command}" 4) "
  [[ ${allowed} == *" ${option} "* ]]
}

reject_option() {
  local option=$1 usage=$2
  echo "error: unknown option: ${option}" >&2
  "${usage}" >&2
  exit 2
}

parse_options() {
  local command=$1 usage=$2
  shift 2
  if [[ ${command} == install && $# -gt 0 ]]; then
    echo "error: the default install takes no arguments: $*" >&2
    usage_root >&2
    exit 2
  fi
  while [[ $# -gt 0 ]]; do
    local option=$1
    case "${option}" in
      -h|--help)
        "${usage}"
        exit 0
        ;;
      --yes|--deploy|--allow-empty-sites|--no-backup)
        option_allowed "${command}" "${option}" || reject_option "${option}" "${usage}"
        # A case, not a chain of `[[ ... ]] && assign`: under `set -e` the last
        # such test to evaluate false is the function's exit status, so that
        # shape only worked because `shift` happened to follow it.
        case "${option}" in
          --yes) parsed_yes=1 ;;
          --deploy) parsed_deploy=1 ;;
          --allow-empty-sites) parsed_allow_empty_sites=1 ;;
          --no-backup) parsed_no_backup=1 ;;
        esac
        if [[ ${command} == fresh && ${option} != --yes ]]; then
          parsed_forward_args+=("${option}")
        fi
        shift
        ;;
      --admin-cidr|--email|--public-address)
        option_allowed "${command}" "${option}" || reject_option "${option}" "${usage}"
        [[ $# -ge 2 ]] || die 2 "error: ${option} needs a value"
        case "${option}" in
          --admin-cidr) parsed_admin_cidr=$2 ;;
          --email) parsed_email=$2 ;;
          --public-address) parsed_public_addresses+=("$2") ;;
        esac
        if [[ ${command} == fresh ]]; then
          parsed_forward_args+=("${option}" "$2")
        fi
        shift 2
        ;;
      *) reject_option "${option}" "${usage}" ;;
    esac
  done
}

# Re-exec this script from a private copy in /tmp, once.
#
# The two removal paths delete the directory this file lives in. A shell reads
# its script lazily, so it would otherwise continue from a path that no longer
# exists. The guard variable is passed
# by name and re-exported, so the copy knows not to copy itself again; that
# second pass is also where the copy arranges to delete itself on exit.
reexec_from_private_copy() {
  local guard=$1 label=$2
  if [[ ${!guard:-0} == 1 ]]; then
    # Registered rather than trapped: a second `trap ... EXIT` here would
    # replace the shared handler, and with it every other path this run has
    # asked to have cleaned up. $0 is already the copy in this pass.
    cleanup_paths+=("$0")
    return 0
  fi
  # Deliberately not temp_path: this copy has to outlive the current process
  # image, because the exec below is what it exists for.
  local copy
  copy=$(mktemp "/tmp/blitzecdn-${label}.XXXXXX")
  install -m 0700 -- "$0" "${copy}"
  exec env "${guard}=1" "${copy}" "${original_args[@]}"
}

# Git against the installation checkout. `safe.directory` is needed because the
# working tree belongs to blitzecdn while `--fresh` runs as root.
repo_git() {
  git -c safe.directory="${INSTALL_DIR}" -C "${INSTALL_DIR}" "$@"
}

# `--fresh` fetches code from origin and then runs it as root, so it accepts
# only the upstream repository rather than trusting an arbitrary remote.
require_upstream_origin() {
  local remote_url
  remote_url=$(repo_git remote get-url origin) ||
    die 1 "error: ${INSTALL_DIR} has no origin remote"
  case "${remote_url}" in
    https://github.com/misaf/blitze-cdn-cp|https://github.com/misaf/blitze-cdn-cp.git|\
    git@github.com:misaf/blitze-cdn-cp.git) ;;
    *) die 1 "error: refusing unexpected origin URL: ${remote_url}" ;;
  esac
  printf '%s\n' "${remote_url}"
}

# Run the private post-bootstrap entry point in the same immutable image as the
# API and worker. It is a one-off operation, so Compose removes the container.
run_install_handoff() {
  docker compose --file "${CONTROL_PLANE_COMPOSE_FILE}" run --rm \
    --entrypoint python \
    -e "BLITZE_ALLOW_EMPTY_SITES=${BLITZE_ALLOW_EMPTY_SITES:-false}" \
    blitzecdn-cli -m blitzecdn.install_handoff "$@"
}

# Converge this host through the control-plane role.
#
# Ansible owns the Linux state here exactly as it owns an edge's: the deployment
# account, explicit container-state ownership, sudo, loopback SSH trust, CLI
# wrapper, environment, runtime image, and Compose project. The installer only
# makes this call possible.
# Run one playbook against this host. The invariant half of both convergences.
#
# Ansible's temporary files must not land in .state. These plays run as root and
# give the persistent state bind mount to the image's non-root account; a
# root-owned leftover there would break the next container invocation. The
# directory is registered rather than removed here, so a play that aborts the
# script still has its scratch space cleaned up.
run_playbook() {
  local playbook=$1
  shift
  local ansible_tmp
  ansible_tmp=$(temp_path -d)
  ANSIBLE_CONFIG="${INSTALL_DIR}/src/blitzecdn/ansible/ansible.cfg" \
    ANSIBLE_LOCAL_TEMP="${ansible_tmp}" \
    "${INSTALL_DIR}/.venv/bin/ansible-playbook" -i localhost, "${playbook}" "$@"
}

converge_control_plane() {
  run_playbook \
    "${INSTALL_DIR}/src/blitzecdn/ansible/playbooks/control-plane.yml" "$@"
}

# Ansible is the only implementation of system teardown. This must finish
# successfully before the installer removes the checkout containing Ansible.
converge_uninstall() {
  local playbook="${INSTALL_DIR}/src/blitzecdn/ansible/playbooks/uninstall.yml"
  local executable="${INSTALL_DIR}/.venv/bin/ansible-playbook"
  [[ -x ${executable} ]] || die 1 \
    "error: cannot uninstall: Ansible is missing at ${executable}" \
    "Repair the installation first, then rerun --uninstall."
  [[ -f ${playbook} ]] || die 1 \
    "error: cannot uninstall: playbook is missing at ${playbook}" \
    "Repair the installation first, then rerun --uninstall."

  # The preconditions are what differ; the run itself is the shared one. Both
  # callers test the status, so the failure must reach them as a return value
  # rather than aborting under `set -e`.
  local status=0
  run_playbook "${playbook}" || status=$?
  return "${status}"
}

# The play above owns every system operation. Bash removes only the checkout
# that contains the now-finished Ansible runtime and its application state.
remove_installation_directory() {
  rm -rf -- "${INSTALL_DIR}"
}

# Hand application-specific wrapper generation to the installed runtime.
handoff_user_wrapper() {
  [[ "${BLITZECDN_USER_WRAPPER:-1}" != 0 ]] || return 0
  local wrapper_dir="${BLITZECDN_WRAPPER_DIR:-${HOME:-}/.local/bin}"
  if [[ -z ${BLITZECDN_WRAPPER_DIR:-} && -z ${HOME:-} ]]; then
    echo "warning: HOME is unset; skipping the ${script_dir}/.venv/bin/blitzecdn shortcut" >&2
    return 0
  fi
  .venv/bin/python -m blitzecdn.install_handoff wrapper \
    --project-dir "${script_dir}" --wrapper-dir "${wrapper_dir}"
  case ":${PATH}:" in
    *":${wrapper_dir}:"*) echo "Run 'blitzecdn' from any directory." ;;
    *)
      local login_rc=.profile
      if [[ ${SHELL##*/} == zsh ]]; then
        login_rc=.zprofile
      fi
      echo "${wrapper_dir}/blitzecdn is not on your PATH yet. Add it with:"
      echo "  echo 'export PATH=\"${wrapper_dir}:\${PATH}\"' >> \"\${HOME}\"/${login_rc}"
      ;;
  esac
}

# ---------------------------------------------------------------------------
# (no arguments) — virtualenv and collections
# ---------------------------------------------------------------------------

# Build only the runtime that every later handoff needs.
bootstrap_runtime() {
  cd -- "${script_dir}"

  local mode="${1:-application}"
  [[ ${mode} == application || ${mode} == ansible-only ]] ||
    die 2 "error: unknown bootstrap runtime mode: ${mode}"

  local python_command="${BLITZECDN_PYTHON:-python3}"
  command -v "${python_command}" >/dev/null 2>&1 ||
    die 1 "error: ${python_command} was not found; install Python 3.12 or newer"

  "${python_command}" -c '
import sys
if sys.version_info[:2] < (3, 12):
    raise SystemExit("error: BlitzeCDN requires Python 3.12 or newer")
'

  local uv
  uv="$(ensure_uv)"

  if [[ -e .venv && ! -x .venv/bin/python ]]; then
    local invalid_venv
    invalid_venv=".venv.invalid.$(date -u +%Y%m%dT%H%M%SZ)"
    echo "warning: .venv is incomplete; preserving it as ${invalid_venv}" >&2
    mv -- .venv "${invalid_venv}"
  fi

  # `uv sync` builds exactly the environment uv.lock describes, which is what
  # makes two servers installed a month apart identical. It also *removes*
  # anything else in .venv, so every runtime requirement has to be a real
  # dependency in pyproject.toml — ansible-core included.
  #
  # --frozen refuses to re-resolve: a lockfile that does not match pyproject
  # fails the install rather than quietly installing something nobody tested.
  #
  # BLITZECDN_DEV=1 adds the test and lint tooling and makes src/ edits take
  # effect without reinstalling. Production standalone/update calls use
  # ansible-only: dependencies still include ansible-core, but the BlitzeCDN
  # project and its CLI are not installed on the host.
  #
  # BLITZECDN_CAPABILITIES lists the optional capabilities this controller
  # installs, as extras on the root project. They are separate distributions
  # under packages/ that the control plane discovers through entry points, so
  # dropping one from this list produces a working controller without that
  # capability and nothing else changes. `backup` is in the default because
  # `update` takes a database backup before it changes anything; a controller
  # installed without it cannot be updated in place. `hardening` is in it
  # because an edge's SSH policy and Fail2Ban jail ship in that distribution:
  # leaving it out is how a fleet whose host access belongs to a golden image
  # or a bastion declines BlitzeCDN's, and leaving it out by accident means no
  # edge is hardened at all. `origins` is in it because `blitzecdn origin
  # check` used to be a core command and an operator upgrading should not
  # discover it missing; it is also what the Automatic SSL/TLS scan probes
  # with, so `certificates` pulls it in regardless. `resolver` is in it for the
  # same upgrade reason and costs nothing to carry: its role is off until
  # `blitzecdn_resolver_enabled` is set, so attaching it manages no host that
  # had not already asked, while leaving it out would silently stop managing
  # the hosts that had.
  local -a sync_flags=(--frozen --python "${python_command}")
  local -a capability_flags=()
  local flag
  while IFS= read -r flag; do
    capability_flags+=("${flag}")
  done < <(capability_extras)
  if [[ "${BLITZECDN_DEV:-0}" == "1" ]]; then
    "${uv}" sync "${sync_flags[@]}" --all-packages
  elif [[ ${mode} == ansible-only ]]; then
    "${uv}" sync "${sync_flags[@]}" --no-dev --no-install-project
  else
    "${uv}" sync "${sync_flags[@]}" --no-dev --no-editable "${capability_flags[@]}"
  fi

  [[ -x .venv/bin/python && -x .venv/bin/ansible-playbook ]] ||
    die 1 "error: uv created an incomplete .venv; rerun with --fresh"
  if [[ ${mode} == application && ! -x .venv/bin/blitzecdn ]]; then
    die 1 "error: uv did not install the local BlitzeCDN CLI"
  fi

  # Collections go inside the repository rather than ~/.ansible/collections:
  # src/blitzecdn/ansible/ansible.cfg no longer names it — it can only point inside the wheel
  # the platform roles ship in — so ANSIBLE_COLLECTIONS_PATH below is the whole
  # of it. tests/test_contract.py reads whatever
  # lands here, and a lab machine can hold several checkouts without them
  # fighting over one global collection path.
  local collections_path=.state/collections

  # Exported rather than left to the config file: since the platform's Ansible
  # moved into the wheel, a relative `collections_path` in src/blitzecdn/ansible/ansible.cfg
  # would resolve inside site-packages. PlaybookExecutor sets the same variable
  # from Settings.state_dir on every run, so galaxy installs where deploys look.
  export ANSIBLE_CONFIG=src/blitzecdn/ansible/ansible.cfg
  export ANSIBLE_COLLECTIONS_PATH="${collections_path}"

  # Only third-party dependencies now: the BlitzeCDN roles live in
  # src/blitzecdn/ansible/roles/ and ship inside the wheel, so there is nothing to pin, to
  # build, or to keep in step with the desired-state contract.
  .venv/bin/ansible-galaxy collection install \
    -r src/blitzecdn/ansible/requirements.yml -p "${collections_path}"

}

cmd_install() {
  bootstrap_runtime
  .venv/bin/blitzecdn setup
  handoff_user_wrapper
  echo
  echo "Installation complete. Run commands with .venv/bin/blitzecdn"
}

# ---------------------------------------------------------------------------
# standalone — provision this server
# ---------------------------------------------------------------------------

usage_standalone() {
  cat <<'EOF'
Usage: sudo ./install.sh standalone --admin-cidr CIDR --email ADDRESS [OPTIONS]

Install one independent BlitzeCDN control plane and edge on this server.

Required:
  --admin-cidr CIDR  Network allowed to administer SSH, for example 203.0.113.8/32
  --email ADDRESS    Default ACME account email

Options:
  --deploy            Run the initial edge deployment (default: prepare only)
  --allow-empty-sites Permit --deploy to remove every previously managed site
  --public-address ADDRESS
                      Public edge IP or hostname; repeat when needed (NAT safe)
  -h, --help         Show this help

The checkout must be /opt/blitzecdn because it is the immutable image build
context. Re-running the installer preserves the API key, inventory, and state.
EOF
}

cmd_standalone() {
  [[ -n ${parsed_admin_cidr} ]] || die 2 "error: --admin-cidr is required"
  [[ -n ${parsed_email} ]] || die 2 "error: --email is required"

  [[ ${script_dir} == "${INSTALL_DIR}" ]] ||
    die 1 "error: clone or copy this release to ${INSTALL_DIR}, then run the installer there"
  cd -- "${script_dir}"

  # Everything up to `converge_control_plane` is bootstrap: the least this host
  # needs before Ansible can run on it. The operating-system check, the rest of
  # the packages, accounts, SSH trust, image, Compose project and environment
  # all belong to the role, which converges them the same way the edge
  # roles converge an edge.
  # python3 is the interpreter uv builds the virtualenv around — uv creates the
  # environment itself, so python3-venv is no longer needed. curl fetches the
  # pinned uv build when the host has no uv of its own; ca-certificates is what
  # makes that fetch verifiable at all, and a minimal image can lack it. git is
  # here because src/blitzecdn/ansible/requirements.yml fetches the edge collection from a Git
  # ref, and that happens in the default install form below — before the role runs.
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates curl git python3

  # Validated as soon as there is an interpreter to validate with, and before
  # the host is converged: a typo then costs a package install rather than a
  # provisioned server that has to be corrected afterwards. A minimal image may
  # genuinely have no python3 until the line above installs it.
  python3 - "${parsed_admin_cidr}" "${parsed_email}" <<'PY'
import ipaddress
import sys

try:
    ipaddress.ip_network(sys.argv[1], strict=False)
except ValueError as error:
    raise SystemExit(f"error: invalid --admin-cidr: {error}") from error
email = sys.argv[2]
if email.count("@") != 1 or any(char.isspace() for char in email):
    raise SystemExit("error: --email must be a valid email address")
PY


  # This virtualenv is bootstrap tooling for Ansible. Long-running application
  # processes are installed into the image built by the role, never the host.
  bootstrap_runtime ansible-only

  # The capability list travels with the convergence. The managed
  # blitzecdn.toml writes a capability's configuration only when that
  # capability is attached, because the control plane refuses to start on
  # configuration no installed capability claims. Under-reporting is safe —
  # an unwritten key leaves the capability's own default in force — which is
  # why BLITZECDN_DEV, which attaches every package, still reports this list.
  local rendered_capabilities
  rendered_capabilities=$(capability_json)

  converge_control_plane \
    --extra-vars "blitzecdn_controlplane_acme_email=${parsed_email}" \
    --extra-vars "{\"blitzecdn_controlplane_capabilities\": [${rendered_capabilities}]}"

  local handoff_args=(standalone --admin-cidr "${parsed_admin_cidr}")
  local public_address
  for public_address in "${parsed_public_addresses[@]}"; do
    handoff_args+=(--public-address "${public_address}")
  done
  if [[ ${parsed_deploy} -eq 1 ]]; then
    handoff_args+=(--deploy)
  fi
  BLITZE_ALLOW_EMPTY_SITES="${parsed_allow_empty_sites}" \
    run_install_handoff "${handoff_args[@]}"

  echo
  echo "Standalone BlitzeCDN installation complete."
  echo "API credentials live in ${CONFIG_DIR}/blitzecdn.env; read it with sudo."
  echo "Connect from your workstation with your existing operator account and key:"
  echo "  ssh -L 8000:127.0.0.1:8000 OPERATOR@THIS_SERVER"
  echo "API:     http://127.0.0.1:8000"
  echo "Status:  docker compose --file ${CONTROL_PLANE_COMPOSE_FILE} ps"
}

# ---------------------------------------------------------------------------
# update — move an installed host onto a newer release
# ---------------------------------------------------------------------------

usage_update() {
  cat <<'EOF'
Usage: sudo ./install.sh update [OPTIONS]

Move this installation onto the newest release of its own major line and leave
its state intact: fetch the code, rebuild the bootstrap tooling and application
image, migrate state, and recreate the Compose services on the new release.

There is nothing to choose. The release line comes from the installed version,
the target is the newest vMAJOR.MINOR.PATCH tag in that line, and the update
never crosses a major version and never moves backwards. It leaves the checkout
detached at that tag, which is what a release installation looks like: a host
installed from the 3.x branch follows tags from its first update onwards, and a
later --fresh reinstalls the exact release that was running rather than the
branch tip. A major upgrade is a separate, documented step.

Nothing is rewritten that an operator owns. The database, the certificates, the
inventory, and the API credentials in /etc/blitzecdn all survive — this is the
non-destructive counterpart to --fresh, which destroys all four.

Options:
  --yes        Do not ask for confirmation
  --no-backup  Skip the database backup taken before anything changes
  -h, --help   Show this help

The checkout must be /opt/blitzecdn, must have the upstream origin, and must
have no local modifications. The persistent containers are stopped while the
schema and immutable image are replaced.
EOF
}

# Print the release version recorded in the checkout at HEAD, or fail.
#
# The major of this version is the release line the host follows. That is sound
# because .github/workflows/ci.yml refuses to build a tag whose name is not
# "v" plus this exact field, so a tag name and a project version are the same
# statement about which release this is.
#
# Read through `git show` rather than off disk: the working tree is verified
# clean before this is reached, so the two agree, and the git form is the one
# the installer's tests can answer. The loop is deliberate — a `grep | head`
# pipeline that matched nothing would abort the whole script under pipefail,
# where an empty read loop just returns 1 for the caller to handle.
head_project_version() {
  local line
  while IFS= read -r line; do
    if [[ ${line} =~ ^version[[:space:]]*=[[:space:]]*\"([0-9]+\.[0-9]+\.[0-9]+)\" ]]; then
      printf '%s\n' "${BASH_REMATCH[1]}"
      return 0
    fi
  done < <(repo_git show HEAD:pyproject.toml 2>/dev/null)
  return 1
}

# Print the newest release tag in one major line, or fail if it has none.
#
# The glob is anchored to the major, so this cannot return a release from
# another line no matter what origin carries. The pattern test is what keeps a
# pre-release or a moving pointer — v3.1.0-rc1, or a branch-shaped v3.x tag —
# out of a server: only a complete vMAJOR.MINOR.PATCH is installable.
latest_release_tag() {
  local major=$1 tag
  while IFS= read -r tag; do
    [[ ${tag} =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
    printf '%s\n' "${tag}"
    return 0
  done < <(repo_git tag --list "v${major}.*" --sort=-v:refname)
  return 1
}

# Print the release this host is on, as an operator would name it.
#
# A tagged checkout is the ordinary case and prints as the bare version. The
# other two forms exist so a host that is *not* on a release still says so
# rather than pretending: a branch checkout reports the release it has moved
# past and by how far, and a checkout with no tag in its history falls back to
# the recorded version and the commit. Never fails — this only ever describes.
describe_installed_release() {
  local tag base drift version sha
  if tag=$(repo_git describe --tags --exact-match HEAD 2>/dev/null); then
    printf '%s\n' "${tag}"
    return 0
  fi
  if base=$(repo_git describe --tags --abbrev=0 HEAD 2>/dev/null); then
    drift=$(repo_git rev-list --count "${base}..HEAD")
    printf '%s (+%s commits)\n' "${base}" "${drift}"
    return 0
  fi
  version=$(head_project_version) || version=unknown
  sha=$(repo_git rev-parse --short HEAD)
  printf 'v%s (%s)\n' "${version}" "${sha}"
}

# Stop persistent containers before replacing the code and image they run.
stop_control_plane_services() {
  [[ -f ${CONTROL_PLANE_COMPOSE_FILE} ]] || return 0
  docker compose --file "${CONTROL_PLANE_COMPOSE_FILE}" stop \
    "${CONTROL_PLANE_SERVICES[@]}"
}

# Report what is running once the role has started it again. The role uses
# `state: started`, so a unit that fails on the new code leaves the play green
# and the service down; this is what makes that visible at the end of a run.
report_control_plane_services() {
  local service health status=0
  for service in "${CONTROL_PLANE_SERVICES[@]}"; do
    health=$(docker inspect --format '{{.State.Health.Status}}' "${service}" 2>/dev/null || true)
    if [[ ${health} == healthy ]]; then
      echo "  ${service}: healthy"
    else
      echo "  ${service}: NOT HEALTHY — inspect 'docker compose --file ${CONTROL_PLANE_COMPOSE_FILE} logs ${service}'" >&2
      status=1
    fi
  done
  return "${status}"
}

confirm_update() {
  local assume_yes="$1" current="$2" target="$3" commits="$4"
  cat >&2 <<EOF

Updating the BlitzeCDN control plane on this host:

  from  ${current}
  to    ${target}   (${commits} commits)

The containers are stopped, the schema is migrated, and they are recreated on
the new image. State is preserved; a migration is not reversible.
EOF
  [[ ${assume_yes} == 1 ]] && return 0
  local answer
  read -r -p "Continue? [y/N]: " answer
  [[ ${answer} =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
}

cmd_update() {

  [[ ${script_dir} == "${INSTALL_DIR}" ]] ||
    die 1 "error: run the updater from the installation at ${INSTALL_DIR}"
  [[ -d ${INSTALL_DIR}/.git ]] || die 1 \
    "error: ${INSTALL_DIR} is not a Git checkout; there is nothing to update from" \
    "Clone the release to ${INSTALL_DIR} and run 'standalone' instead."
  cd -- "${script_dir}"

  # An update fetches code and then runs it as root, exactly as --fresh does,
  # so it accepts only the upstream repository.
  require_upstream_origin >/dev/null

  # Local edits would be silently destroyed by the checkout below, and an
  # operator who has patched a server is the one person who most needs to be
  # told before that happens.
  [[ -z $(repo_git status --porcelain) ]] || die 1 \
    "error: ${INSTALL_DIR} has local modifications; the update would discard them" \
    "Commit or discard them, or use --fresh to rebuild from origin."

  echo "Fetching from origin..."
  repo_git fetch --tags --prune origin ||
    die 1 "error: could not fetch from origin; nothing was changed"

  # There is nothing for an operator to choose here. A host follows its own
  # release line and the newest tag in it: crossing a major line is a migration
  # somebody opts into deliberately, not something an updater performs, and
  # following a branch tip would install commits no release was ever cut from.
  local version major target current commits
  version=$(head_project_version) || die 1 \
    "error: could not read the project version from pyproject.toml at HEAD; nothing was changed"
  major=${version%%.*}

  target=$(latest_release_tag "${major}") || die 1 \
    "error: no v${major}.x release tag exists in the fetched repository; nothing was changed" \
    "Only tagged releases are installable. Check 'git -C ${INSTALL_DIR} tag --list'."

  current=$(describe_installed_release)

  # Only an exact tag can equal the target: the other two descriptions carry a
  # commit count or a SHA, so this is precisely "HEAD is the newest release".
  if [[ ${current} == "${target}" ]]; then
    echo "Already on ${target}; nothing to update."
    return 0
  fi

  # Forward only. A checkout that is not an ancestor of the release has commits
  # origin does not, or is already past it, and moving it would discard them —
  # --fresh is the tool that rebuilds such a host from origin. Asked here rather
  # than at the checkout below so the refusal costs no downtime; the equivalent
  # question used to be answered by a fast-forward merge refusing, after the
  # containers had already been stopped.
  repo_git merge-base --is-ancestor HEAD "${target}^{commit}" || die 1 \
    "error: ${INSTALL_DIR} is not an ancestor of ${target}; nothing was changed" \
    "The checkout has commits origin does not, or is on a newer release." \
    "Use --fresh to rebuild from origin."

  commits=$(repo_git rev-list --count "HEAD..${target}")

  confirm_update "${parsed_yes}" "${current}" "${target}" "${commits}"

  # Before anything is touched, and through the wrapper so it runs as the
  # image's non-root account against the service environment. The database component
  # uses VACUUM INTO, so this is safe against a controller that is still
  # serving. Only the database: an update replaces code, never certificates or
  # credentials, so a full backup here would cost minutes and protect nothing
  # this operation can damage.
  if [[ ${parsed_no_backup} -eq 1 ]]; then
    echo "Skipping the database backup (--no-backup)."
  elif [[ -x ${CLI_WRAPPER} ]]; then
    echo "Backing up the database..."
    "${CLI_WRAPPER}" backup create --only database ||
      die 1 "error: the database backup failed; nothing was changed" \
        "Fix the failure, or rerun with --no-backup if the data is expendable."
  else
    echo "warning: ${CLI_WRAPPER} is missing; skipping the database backup" >&2
  fi

  # Downtime starts here. Stopping first is what keeps the schema migration in
  # the converge below from running underneath processes still executing the
  # release it is migrating away from.
  stop_control_plane_services

  echo "Checking out ${target}..."
  # A tag checks out detached, which is what a release installation looks like
  # and what --fresh reads back to reinstall the same release. Never a merge, a
  # pull, a reset or a force: an update moves a server forward onto a release it
  # has already been shown to be behind, or it refuses above.
  repo_git checkout --quiet "${target}" ||
    die 1 "error: could not check out ${target}; the containers are stopped —" \
      "restore with 'git -C ${INSTALL_DIR} checkout ${current}' and rerun"

  # Same two steps a standalone install runs, in the same order and through the
  # same functions: bootstrap tooling from the new lockfile, then the role,
  # which owns image build, schema migration, and Compose service recreation.
  bootstrap_runtime ansible-only
  converge_control_plane

  echo
  echo "Updated to ${target}."
  echo "Services:"
  report_control_plane_services
}

# ---------------------------------------------------------------------------
# uninstall / fresh — remove every artifact, or wipe and rebuild clean
# ---------------------------------------------------------------------------

# Everything the two removal paths delete, spelled once.
#
# This list is read twice: in `--uninstall --help`, and in the confirmation an
# operator answers before an irreversible action. Those two drifted while they
# were separate copies — the help grew the cache and the nginx drop-ins, and
# the confirmation, the one that actually guards the action, did not. One
# function means the prompt cannot again understate what is about to happen.
removal_manifest() {
  cat <<'EOF'
  - the installation directory /opt/blitzecdn and everything in it: source,
    .venv, .env, and .state (database, certificates, collections, locks, logs)
  - the control-plane Compose project and BlitzeCDN host timers
  - /etc/blitzecdn and the API credentials it holds
  - the /usr/local/bin/blitzecdn command
  - the sudo rule /etc/sudoers.d/blitzecdn-deploy
  - the deploy account and its home
  - the edge-managed nginx sites, certificates, cache, and drop-ins this host
    converged as an edge
EOF
}

usage_uninstall() {
  cat <<'EOF'
Usage: sudo ./install.sh --uninstall [OPTIONS]

Remove every BlitzeCDN artifact from this host and stop there, leaving the
system packages it had and nothing BlitzeCDN wrote.

Options:
  --yes        Do not ask for confirmation
  -h, --help   Show this help

Removed:
EOF
  removal_manifest
  cat <<'EOF'

Nothing outside those paths is changed and no packages are removed. Ansible
must still be available in the installation: it performs all system teardown.
The installer then removes its own checkout from a private copy.
EOF
}

usage_fresh() {
  cat <<'EOF'
Usage: sudo ./install.sh --fresh [OPTIONS] [STANDALONE OPTIONS]

Uninstall BlitzeCDN completely, then install it again exactly as a brand-new
server is installed: clone the running release from origin and run the
standalone installer. The standalone options pass through unchanged:

  --admin-cidr CIDR  Network allowed to administer SSH
  --email ADDRESS    Default ACME account email
  --public-address ADDRESS
                     Public edge IP or hostname; repeat when needed
  --deploy           Run the initial edge deployment (default: prepare only)
  --allow-empty-sites
                     Permit --deploy to remove every previously managed site

Options:
  --yes        Do not ask for confirmation before uninstalling
  -h, --help   Show this help

The current checkout must be a Git clone whose origin is the upstream
repository, so the rebuild reinstalls the exact release that is running. Run
--uninstall instead to remove BlitzeCDN without installing it again.
EOF
}

confirm_destructive() {
  local assume_yes="$1" mode="$2"
  [[ ${assume_yes} == 1 ]] && return 0
  echo >&2 "This removes every BlitzeCDN artifact on this host and cannot be undone:"
  echo >&2
  removal_manifest >&2
  if [[ ${mode} == fresh ]]; then
    echo >&2 "Afterwards it re-clones the current release and reinstalls it."
  fi
  local answer
  read -r -p "Continue? [y/N]: " answer
  [[ ${answer} =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
}

cmd_uninstall() {

  # The cleanup deletes the directory this file lives in.
  reexec_from_private_copy BLITZECDN_UNINSTALL_REEXEC uninstall

  confirm_destructive "${parsed_yes}" uninstall
  converge_uninstall
  remove_installation_directory

  echo
  echo "BlitzeCDN has been removed. No packages were removed and nothing outside"
  echo "the paths listed in the confirmation was changed."
}

cmd_fresh() {

  # Same reason as --uninstall, and it shares the guard: --fresh runs that
  # cleanup before it re-clones.
  reexec_from_private_copy BLITZECDN_UNINSTALL_REEXEC fresh

  # The re-clone needs the origin and the running release, so read them from
  # the checkout before the cleanup removes it.
  [[ -d ${INSTALL_DIR}/.git ]] || die 1 \
    "error: ${INSTALL_DIR} is not a Git checkout; nothing to reinstall from" \
    "Run --uninstall instead, or clone the release to ${INSTALL_DIR} first."

  local remote_url
  remote_url=$(require_upstream_origin)

  # Preserve the source identity of the running checkout. An exact release tag
  # stays on that release, the supported 3.x line stays attached to that branch,
  # and any other development checkout is pinned to its exact commit. All of
  # this is read before the cleanup, because the cleanup deletes the checkout.
  local revision
  revision=$(repo_git describe --tags --exact-match HEAD 2>/dev/null || true)
  local named_revision=1
  if [[ -z ${revision} ]]; then
    revision=$(repo_git symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    if [[ ${revision} != "3.x" ]]; then
      revision=$(repo_git rev-parse HEAD)
      named_revision=0
    fi
  fi

  confirm_destructive "${parsed_yes}" fresh

  # Fetch and verify the replacement before destroying the working controller.
  # A network outage or missing ref must leave the current installation wholly
  # usable, not turn `--fresh` into a successful uninstall followed by a failed
  # clone. The staging directory shares /opt with the final checkout, so the
  # handoff after teardown is a same-filesystem rename.
  # Registered, so every refusal below simply reports: the staged clone is
  # removed on the way out whether it was this run's own `die` that ended
  # things or an abort nobody wrote a handler for.
  local staging
  staging=$(temp_path -d "${INSTALL_DIR%/*}/.blitzecdn-fresh.XXXXXX")
  if [[ ${named_revision} -eq 1 ]]; then
    git clone --branch "${revision}" "${remote_url}" "${staging}" ||
      die 1 "error: could not stage release ${revision}; the current installation was not changed"
  else
    if ! git clone "${remote_url}" "${staging}" ||
      ! git -C "${staging}" checkout --detach "${revision}"; then
      die 1 "error: could not stage commit ${revision}; the current installation was not changed"
    fi
  fi
  [[ -x ${staging}/install.sh ]] ||
    die 1 "error: staged release has no executable install.sh; the current installation was not changed"

  converge_uninstall ||
    die 1 "error: teardown failed; the current checkout remains, but inspect and repair any partially removed host state before retrying"
  remove_installation_directory
  # The host has no installation from here until the rename lands, so the
  # staged clone is no longer disposable.
  unregister_path "${staging}"
  mv -- "${staging}" "${INSTALL_DIR}" ||
    die 1 "error: teardown succeeded but the staged release remains at ${staging}; move it to ${INSTALL_DIR} and rerun standalone"

  echo
  echo "Reinstalling the running release from a clean state..."
  # `${parsed_forward_args[@]+...}` keeps this legal on bash 3.2 (macOS), where an empty
  # array expands to an unbound-variable error under `set -u`.
  "${INSTALL_DIR}/install.sh" standalone \
    ${parsed_forward_args[@]+"${parsed_forward_args[@]}"}
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

main() {
  local subcommand=install
  if [[ $# -gt 0 ]]; then
    case "$1" in
      standalone|update|install)
        subcommand=$1
        shift
        ;;
      --uninstall)
        subcommand=uninstall
        shift
        ;;
      --fresh)
        subcommand=fresh
        shift
        ;;
      help|-h|--help)
        usage_root
        exit 0
        ;;
    esac
  fi

  local usage root_label
  usage=$(command_field "${subcommand}" 2)
  root_label=$(command_field "${subcommand}" 3)

  # Parsing first, so `--help` still answers without sudo. The privileged
  # commands used to open with their own require_root call; the table says it
  # once, and says it for a command that forgets to.
  parse_options "${subcommand}" "${usage}" "$@"
  [[ -z ${root_label} ]] || require_root "${root_label}"

  # No arguments: every cmd_* reads the parsed globals, and passing the
  # leftovers implied a contract none of them had.
  "cmd_${subcommand}"
}

# Sourcing this file defines its functions and runs nothing, so the helpers can
# be exercised directly instead of through a subprocess that provisions a host.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
