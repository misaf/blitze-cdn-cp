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
parsed_ref=''
parsed_public_addresses=()
parsed_forward_args=()

option_allowed() {
  local command=$1 option=$2
  case "${command}:${option}" in
    standalone:--admin-cidr|standalone:--email|standalone:--deploy|\
    standalone:--allow-empty-sites|standalone:--public-address|\
    fresh:--admin-cidr|fresh:--email|fresh:--deploy|fresh:--allow-empty-sites|\
    fresh:--public-address|fresh:--yes|uninstall:--yes|\
    update:--ref|update:--yes|update:--no-backup) return 0 ;;
    *) return 1 ;;
  esac
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
        [[ ${option} == --yes ]] && parsed_yes=1
        [[ ${option} == --deploy ]] && parsed_deploy=1
        [[ ${option} == --allow-empty-sites ]] && parsed_allow_empty_sites=1
        [[ ${option} == --no-backup ]] && parsed_no_backup=1
        [[ ${command} == fresh && ${option} != --yes ]] &&
          parsed_forward_args+=("${option}")
        shift
        ;;
      --admin-cidr|--email|--public-address|--ref)
        option_allowed "${command}" "${option}" || reject_option "${option}" "${usage}"
        [[ $# -ge 2 ]] || die 2 "error: ${option} needs a value"
        case "${option}" in
          --admin-cidr) parsed_admin_cidr=$2 ;;
          --email) parsed_email=$2 ;;
          --ref) parsed_ref=$2 ;;
          --public-address) parsed_public_addresses+=("$2") ;;
        esac
        [[ ${command} == fresh ]] && parsed_forward_args+=("${option}" "$2")
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
    # Deferred expansion: $0 is the copy, and functions do not change it.
    trap 'rm -f -- "$0"' EXIT
    return 0
  fi
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
converge_control_plane() {
  local ansible_tmp status=0
  ansible_tmp=$(mktemp -d)
  # Ansible's temporary files must not land in .state. This play runs as root
  # and gives the persistent state bind mount to the image's non-root account;
  # a root-owned leftover there would break the next container invocation.
  ANSIBLE_CONFIG=ansible/ansible.cfg ANSIBLE_LOCAL_TEMP="${ansible_tmp}" \
    "${INSTALL_DIR}/.venv/bin/ansible-playbook" \
    -i localhost, ansible/playbooks/control-plane.yml "$@" || status=$?
  rm -rf -- "${ansible_tmp}"
  return "${status}"
}

# Ansible is the only implementation of system teardown. This must finish
# successfully before the installer removes the checkout containing Ansible.
converge_uninstall() {
  local playbook="${INSTALL_DIR}/ansible/playbooks/uninstall.yml"
  local executable="${INSTALL_DIR}/.venv/bin/ansible-playbook"
  [[ -x ${executable} ]] || die 1 \
    "error: cannot uninstall: Ansible is missing at ${executable}" \
    "Repair the installation first, then rerun --uninstall."
  [[ -f ${playbook} ]] || die 1 \
    "error: cannot uninstall: playbook is missing at ${playbook}" \
    "Repair the installation first, then rerun --uninstall."

  local ansible_tmp status=0
  ansible_tmp=$(mktemp -d)
  ANSIBLE_CONFIG="${INSTALL_DIR}/ansible/ansible.cfg" ANSIBLE_LOCAL_TEMP="${ansible_tmp}" \
    "${executable}" -i localhost, "${playbook}" || status=$?
  rm -rf -- "${ansible_tmp}"
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
      [[ ${SHELL##*/} == zsh ]] && login_rc=.zprofile
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
  local -a sync_flags=(--frozen --python "${python_command}")
  if [[ "${BLITZECDN_DEV:-0}" == "1" ]]; then
    "${uv}" sync "${sync_flags[@]}"
  elif [[ ${mode} == ansible-only ]]; then
    "${uv}" sync "${sync_flags[@]}" --no-dev --no-install-project
  else
    "${uv}" sync "${sync_flags[@]}" --no-dev --no-editable
  fi

  [[ -x .venv/bin/python && -x .venv/bin/ansible-playbook ]] ||
    die 1 "error: uv created an incomplete .venv; rerun with --fresh"
  if [[ ${mode} == application && ! -x .venv/bin/blitzecdn ]]; then
    die 1 "error: uv did not install the local BlitzeCDN CLI"
  fi

  # Collections go inside the repository rather than ~/.ansible/collections:
  # ansible/ansible.cfg looks here first, tests/test_contract.py reads whatever
  # lands here, and a lab machine can hold several checkouts without them
  # fighting over one global collection path.
  local collections_path=.state/collections

  # ansible/ansible.cfg already resolves collections_path to that directory
  # (relative entries there resolve against the config file, not the caller's
  # working directory). Exporting it keeps ansible-galaxy from warning that it is
  # installing somewhere Ansible will not look, which is not true at deploy time.
  export ANSIBLE_CONFIG=ansible/ansible.cfg

  # Only third-party dependencies now: the BlitzeCDN roles live in
  # ansible/roles/ and ship with this checkout, so there is nothing to pin, to
  # build, or to keep in step with the desired-state contract.
  .venv/bin/ansible-galaxy collection install \
    -r ansible/requirements.yml -p "${collections_path}"

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
  require_root
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
  # here because ansible/requirements.yml fetches the edge collection from a Git
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

  converge_control_plane \
    --extra-vars "blitzecdn_controlplane_acme_email=${parsed_email}"

  local handoff_args=(standalone --admin-cidr "${parsed_admin_cidr}")
  local public_address
  for public_address in "${parsed_public_addresses[@]}"; do
    handoff_args+=(--public-address "${public_address}")
  done
  [[ ${parsed_deploy} -eq 1 ]] && handoff_args+=(--deploy)
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

Move this installation onto a newer release and leave its state intact: fetch
the code, rebuild the bootstrap tooling and application image, migrate state,
and recreate the Compose services on the new release.

Nothing is rewritten that an operator owns. The database, the certificates, the
inventory, and the API credentials in /etc/blitzecdn all survive — this is the
non-destructive counterpart to --fresh, which destroys all four.

Options:
  --ref REF    Tag or branch to update to (default: the tracked branch)
  --yes        Do not ask for confirmation
  --no-backup  Skip the database backup taken before anything changes
  -h, --help   Show this help

The checkout must be /opt/blitzecdn, must have the upstream origin, and must
have no local modifications. The persistent containers are stopped while the
schema and immutable image are replaced.
EOF
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
  to    ${target}  (${commits} commit(s) ahead)

The containers are stopped, the schema is migrated, and they are recreated on
the new image. State is preserved; a migration is not reversible.
EOF
  [[ ${assume_yes} == 1 ]] && return 0
  local answer
  read -r -p "Continue? [y/N]: " answer
  [[ ${answer} =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
}

cmd_update() {
  require_root updater

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

  # An explicit ref wins. Without one the tracked branch is the only safe
  # default: a checkout pinned to a tag has no "next" release to infer, and
  # guessing the newest tag would upgrade a host across a major line.
  local target
  if [[ -n ${parsed_ref} ]]; then
    target=${parsed_ref}
  else
    local branch
    branch=$(repo_git symbolic-ref --quiet --short HEAD 2>/dev/null) || die 1 \
      "error: ${INSTALL_DIR} is not on a branch, so there is no tracked release to follow" \
      "Pass --ref TAG or --ref BRANCH to say what to update to."
    target="origin/${branch}"
  fi

  repo_git rev-parse --verify --quiet "${target}^{commit}" >/dev/null ||
    die 1 "error: ${target} does not exist in the fetched repository; nothing was changed"

  local current_description target_description commits
  current_description=$(repo_git describe --tags --always HEAD)
  target_description=$(repo_git describe --tags --always "${target}")
  commits=$(repo_git rev-list --count "HEAD..${target}")

  if [[ ${commits} -eq 0 ]] &&
    [[ $(repo_git rev-parse HEAD) == $(repo_git rev-parse "${target}^{commit}") ]]; then
    echo "Already on ${target_description}; nothing to update."
    return 0
  fi

  confirm_update "${parsed_yes}" \
    "${current_description}" "${target_description}" "${commits}"

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

  echo "Checking out ${target_description}..."
  if [[ -n ${parsed_ref} ]]; then
    # A tag checks out detached, which is what a release installation looks
    # like and what --fresh reads back to reinstall the same release.
    repo_git checkout --quiet "${parsed_ref}" ||
      die 1 "error: could not check out ${parsed_ref}; the containers are stopped — " \
        "restore with 'git -C ${INSTALL_DIR} checkout ${current_description}' and rerun"
  else
    # Fast-forward only: an update must never quietly merge or rewrite history
    # on a server, and a branch that cannot fast-forward means the checkout has
    # commits origin does not, which --fresh is the tool for.
    repo_git merge --ff-only "${target}" ||
      die 1 "error: ${INSTALL_DIR} cannot fast-forward to ${target}" \
        "The checkout has commits origin does not. Use --fresh to rebuild from origin."
  fi

  # Same two steps a standalone install runs, in the same order and through the
  # same functions: bootstrap tooling from the new lockfile, then the role,
  # which owns image build, schema migration, and Compose service recreation.
  bootstrap_runtime ansible-only
  converge_control_plane

  echo
  echo "Updated to ${target_description}."
  echo "Services:"
  report_control_plane_services
}

# ---------------------------------------------------------------------------
# uninstall / fresh — remove every artifact, or wipe and rebuild clean
# ---------------------------------------------------------------------------

usage_uninstall() {
  cat <<'EOF'
Usage: sudo ./install.sh --uninstall [OPTIONS]

Remove every BlitzeCDN artifact from this host and stop there, leaving the
system packages it had and nothing BlitzeCDN wrote.

Options:
  --yes        Do not ask for confirmation
  -h, --help   Show this help

Removed:
  - the installation directory /opt/blitzecdn and everything in it: source,
    .venv, .env, and .state (database, certificates, collections, locks, logs)
  - the control-plane Compose project and BlitzeCDN host timers
  - /etc/blitzecdn and the API credentials it holds
  - the /usr/local/bin/blitzecdn command
  - the sudo rule /etc/sudoers.d/blitzecdn-deploy
  - the deploy account and its home
  - the edge-managed nginx sites, certificates, cache, and drop-ins this host
    converged as an edge

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
EOF
}

confirm_destructive() {
  local assume_yes="$1" mode="$2"
  [[ ${assume_yes} == 1 ]] && return 0
  cat >&2 <<'EOF'
This removes every BlitzeCDN artifact on this host and cannot be undone:

  - /opt/blitzecdn and all state in it (database, certificates, collections)
  - the control-plane Compose project and BlitzeCDN host timers
  - /etc/blitzecdn and the API credentials it holds
  - the /usr/local/bin/blitzecdn command
  - the sudo rule /etc/sudoers.d/blitzecdn-deploy
  - the deploy account and its home
  - the edge-managed nginx sites and certificates on this host
EOF
  if [[ ${mode} == fresh ]]; then
    echo >&2 "Afterwards it re-clones the current release and reinstalls it."
  fi
  local answer
  read -r -p "Continue? [y/N]: " answer
  [[ ${answer} =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
}

cmd_uninstall() {
  require_root uninstaller

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
  require_root

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
  # stays on that release, the supported 2.x line stays attached to that branch,
  # and any other development checkout is pinned to its exact commit. All of
  # this is read before the cleanup, because the cleanup deletes the checkout.
  local revision
  revision=$(repo_git describe --tags --exact-match HEAD 2>/dev/null || true)
  local named_revision=1
  if [[ -z ${revision} ]]; then
    revision=$(repo_git symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    if [[ ${revision} != "2.x" ]]; then
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
  local staging
  staging=$(mktemp -d "${INSTALL_DIR%/*}/.blitzecdn-fresh.XXXXXX")
  if [[ ${named_revision} -eq 1 ]]; then
    if ! git clone --branch "${revision}" "${remote_url}" "${staging}"; then
      rm -rf -- "${staging}"
      die 1 "error: could not stage release ${revision}; the current installation was not changed"
    fi
  else
    if ! git clone "${remote_url}" "${staging}" ||
      ! git -C "${staging}" checkout --detach "${revision}"; then
      rm -rf -- "${staging}"
      die 1 "error: could not stage commit ${revision}; the current installation was not changed"
    fi
  fi
  if [[ ! -x ${staging}/install.sh ]]; then
    rm -rf -- "${staging}"
    die 1 "error: staged release has no executable install.sh; the current installation was not changed"
  fi

  if ! converge_uninstall; then
    rm -rf -- "${staging}"
    die 1 "error: teardown failed; the current checkout remains, but inspect and repair any partially removed host state before retrying"
  fi
  remove_installation_directory
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

subcommand=install
if [[ $# -gt 0 ]]; then
  case "$1" in
    standalone|update)
      subcommand=$1
      shift
      ;;
    install)
      subcommand=install
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

case "${subcommand}" in
  install) parse_options install usage_root "$@" ;;
  standalone) parse_options standalone usage_standalone "$@" ;;
  update) parse_options update usage_update "$@" ;;
  uninstall) parse_options uninstall usage_uninstall "$@" ;;
  fresh) parse_options fresh usage_fresh "$@" ;;
esac

"cmd_${subcommand}" "$@"
