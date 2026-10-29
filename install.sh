#!/usr/bin/env bash
# BlitzeCDN installer: the bootstrap that gets a host to where Ansible and the
# stdlib helpers under src/blitzecdn/host/ can do the rest.
#
#   (no arguments)  build the virtualenv and install the pinned collections
#   standalone      provision this whole server as control plane and edge, from
#                   the published control-plane image: no Python package is
#                   installed on the host, and nothing is fetched from PyPI
#   update          move an installed host onto a newer release
#   --uninstall     remove every BlitzeCDN artifact
#
# A server needs only this file. Its release is the image tag below, and the
# installation puts a copy of the release's own installer on the host as
# blitzecdn-install, which every later update and uninstall runs.
set -Eeuo pipefail

readonly CONFIG_DIR=/etc/blitzecdn
readonly CLI_WRAPPER=/usr/local/bin/blitzecdn
readonly INSTALLER_COMMAND=/usr/local/sbin/blitzecdn-install
readonly CONTROL_PLANE_COMPOSE_FILE=/etc/blitzecdn/control-plane.compose.yml
# The control-plane image this release installs, moved with the version by
# `just release-version`. The image tag is the release: an update reads the
# installed one back from the Compose project and moves it forward.
readonly CONTROL_PLANE_IMAGE=ghcr.io/misaf/blitzecdn-control-plane:6.1.0
# `blitzecdn.assets.docker.CONTROL_PLANE_DOCKERFILE`, for a source build.
readonly CONTROL_PLANE_DOCKERFILE=src/blitzecdn/assets/docker/control-plane/Dockerfile
readonly CONTROL_PLANE_SOURCE_TAG=blitzecdn-control-plane:local
# Where the image carries the tree it was built from, playbooks included.
readonly IMAGE_ANSIBLE_DIR=/opt/blitzecdn/src/blitzecdn/assets/ansible
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

Run './install.sh SUBCOMMAND --help' for the options of a subcommand. On an
installed server the same commands are `sudo blitzecdn-install SUBCOMMAND`.

Environment for the no-argument form:
  BLITZECDN_PYTHON     Python interpreter to build .venv with (default python3)
  BLITZECDN_DEV=1      Install -e '.[dev]' instead of a plain wheel
  BLITZECDN_WRAPPER_DIR
                       Where to put the `blitzecdn` command (default ~/.local/bin)
  BLITZECDN_USER_WRAPPER=0
                       Do not put `blitzecdn` on PATH; use .venv/bin/blitzecdn

The no-argument form installs a `blitzecdn` command that pins this checkout as
the project directory, so it can be run from any working directory.

Environment for standalone and update:
EOF
  usage_image_environment
}

# Shared by every help that provisions from the control-plane image.
usage_image_environment() {
  cat <<'EOF'
  BLITZECDN_CONTROL_PLANE_IMAGE
                       Run this image instead of the one the release names, for
                       example with @sha256:DIGEST added, or from a mirror. Its
                       tag must be the release version.
  BLITZECDN_CONTROL_PLANE_ARCHIVE
                       Load the image from this `docker save` archive instead
                       of pulling it, for a host without registry access
  BLITZECDN_CONTROL_PLANE_BUILD=1
                       standalone only: build the image from the source
                       checkout this script is in; needs Docker Hub and PyPI
EOF
}

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Fail with one or more stderr lines. The first argument is the exit status:
# 2 is invalid input, 1 a host that cannot be installed onto.
die() {
  local status=$1
  shift
  printf '%s\n' "$@" >&2
  exit "${status}"
}

# Temporary paths this run removes however it ends. The emptiness guard is for
# Bash 3.2, where expanding an empty array under `set -u` is an error.
cleanup_paths=()
on_exit() {
  [[ ${#cleanup_paths[@]} -eq 0 ]] || rm -rf -- "${cleanup_paths[@]}"
}
trap on_exit EXIT

# The pinned uv release and each build's checksum, kept in this repository so
# the download is checked against what this release was tested with. Refresh
# with `just uv-pin VERSION`. macOS builds exist for controller-only checkouts.
UV_VERSION="0.12.3"
# shellcheck disable=SC2034 # read by indirect expansion in ensure_uv
UV_SHA256_x86_64_unknown_linux_gnu="600cf9a742aca00d292673b16b5acffaa7b8c269a364ad0c2e79498dcb1fe101"
# shellcheck disable=SC2034 # read by indirect expansion in ensure_uv
UV_SHA256_aarch64_unknown_linux_gnu="bb66cb52e7b1823aed1183630d8d8e5c958840d584a4c55ec10a4cfc168dcca2"
# shellcheck disable=SC2034 # read by indirect expansion in ensure_uv
UV_SHA256_x86_64_apple_darwin="4c9f52262a14da336e4a42ed24992d12d0c956acde87619e4611d321dffa602b"
# shellcheck disable=SC2034 # read by indirect expansion in ensure_uv
UV_SHA256_aarch64_apple_darwin="546f7f8a6c70ff13a3a9d2bc958db3427298cebf3e0cb756f9177133b7068843"

# Print the SHA-256 of a file: `sha256sum` on servers, `shasum` on macOS.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    die 1 "error: need sha256sum or shasum to verify the uv download"
  fi
}

# Print the path to a usable uv: the operator's own if at least the pinned
# version, otherwise the pinned build unpacked into .state/bin.
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

  # Both halves of the target triple: the OS as well as the architecture.
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

  # Cleaned up here: this runs in a `$(...)` subshell.
  local archive
  archive="$(mktemp)"

  # stderr: stdout is this function's return value.
  echo "Downloading uv ${UV_VERSION} (${target})..." >&2
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

# Guard a privileged subcommand. One line, which the test sandbox neutralises.
require_root() {
  [[ ${EUID} -eq 0 ]] || die 1 "error: run this ${1:-installer} with sudo"
}

# Parsed options are globals: Bash 3.2 (macOS) has no associative arrays or
# namerefs.
parsed_yes=0
parsed_admin_cidr=''
parsed_email=''
parsed_deploy=0
parsed_allow_empty_sites=0
parsed_no_backup=0
parsed_major=0
parsed_public_addresses=()
parsed_allowed_ips=()

# Every subcommand: name, usage function, the sudo label (empty when root is
# not needed), and the options it takes.
readonly COMMAND_TABLE='
install|usage_root||
standalone|usage_standalone|installer|--admin-cidr --email --deploy --allow-empty-sites --public-address --allowed-ips
update|usage_update|updater|--yes --no-backup --major --allowed-ips
uninstall|usage_uninstall|uninstaller|--yes
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
      --yes|--deploy|--allow-empty-sites|--no-backup|--major)
        option_allowed "${command}" "${option}" || reject_option "${option}" "${usage}"
        case "${option}" in
          --yes) parsed_yes=1 ;;
          --deploy) parsed_deploy=1 ;;
          --allow-empty-sites) parsed_allow_empty_sites=1 ;;
          --no-backup) parsed_no_backup=1 ;;
          --major) parsed_major=1 ;;
        esac
        shift
        ;;
      --admin-cidr|--email|--public-address|--allowed-ips)
        option_allowed "${command}" "${option}" || reject_option "${option}" "${usage}"
        [[ $# -ge 2 ]] || die 2 "error: ${option} needs a value"
        case "${option}" in
          --admin-cidr) parsed_admin_cidr=$2 ;;
          --email) parsed_email=$2 ;;
          --public-address) parsed_public_addresses+=("$2") ;;
          --allowed-ips) parsed_allowed_ips+=("$2") ;;
        esac
        shift 2
        ;;
      *) reject_option "${option}" "${usage}" ;;
    esac
  done
}

# Run one of the `blitzecdn.host` helpers in a control-plane image. The host
# has no copy of them: this script is the only BlitzeCDN file it is given.
# The host's network, so a registry lookup sees what the host sees.
run_image_helper() {
  local image=$1 module=$2
  shift 2
  docker run --rm --network=host --entrypoint python "${image}" \
    -I -m "${module}" "$@"
}

# Validate operator input in the image that is about to act on it, before the
# host is converged.
check_inputs() {
  local image=$1
  shift
  run_image_helper "${image}" blitzecdn.host.install_inputs "$@" || exit 2
}

# Run the post-bootstrap entry point in the control-plane image.
run_install_handoff() {
  docker compose --file "${CONTROL_PLANE_COMPOSE_FILE}" run --rm \
    --entrypoint python \
    -e "BLITZE_ALLOW_EMPTY_SITES=${BLITZE_ALLOW_EMPTY_SITES:-false}" \
    blitzecdn-cli -m blitzecdn.entrypoints.install_handoff "$@"
}

# Run one of the image's own playbooks against this host.
#
# Ansible, its collections and the playbook are the image's, so the host needs
# no Python packages. The plays reach the host through the nsenter connection,
# which is why the container is privileged and shares the host's PID
# namespace; the network is the host's so a task that fetches something sees
# what the host sees. Nothing is mounted: every file a task touches is written
# on the host through that connection.
run_playbook() {
  local image=$1 playbook=$2
  shift 2
  docker run --rm --privileged --pid=host --network=host --user 0:0 \
    --env HOME=/root \
    --env ANSIBLE_CONFIG="${IMAGE_ANSIBLE_DIR}/ansible.cfg" \
    --env ANSIBLE_LOCAL_TEMP=/tmp/ansible-local \
    --entrypoint ansible-playbook "${image}" \
    -i localhost, "${IMAGE_ANSIBLE_DIR}/playbooks/${playbook}" "$@"
}

# Whether this host can be the edge half of a standalone installation.
edge_platform_supported() {
  local id=$1 version=$2
  [[ ${id} == ubuntu && ${version} == 26.04 ]]
}

# Refuse a standalone installation this host cannot finish, before apt.
#
# The control-plane role accepts Debian 13+ and Ubuntu 24.04+, the edge play
# only Ubuntu 26.04, and a standalone host is both. `update` converges the
# control plane alone, so it does not call this.
# shellcheck disable=SC1091 # /etc/os-release is the host's, not this repository's
require_edge_platform() {
  [[ -r /etc/os-release ]] ||
    die 1 "error: cannot identify this operating system: /etc/os-release is unreadable"
  local id version
  id=$(. /etc/os-release && printf %s "${ID:-}")
  version=$(. /etc/os-release && printf %s "${VERSION_ID:-}")
  edge_platform_supported "${id}" "${version}" || die 1 \
    "error: a standalone installation requires Ubuntu 26.04 LTS; this host is ${id:-unknown} ${version:-unknown}" \
    "A standalone server is its own edge, and edges for this release require Ubuntu 26.04." \
    "To use this host as a controller only, run ./install.sh with no arguments and register a separate Ubuntu 26.04 edge."
}

# Validate `--allowed-ips` in the given image and print the single
# comma-separated value the role takes; empty when the flag was not given.
# Called before anything changes.
allowed_ips_value() {
  local image=$1 joined='' entry
  for entry in ${parsed_allowed_ips[@]+"${parsed_allowed_ips[@]}"}; do
    joined="${joined:+${joined},}${entry}"
  done
  if [[ -n ${joined} ]]; then
    check_inputs "${image}" allowed-ips "${joined}" >&2
  fi
  printf '%s' "${joined}"
}

# Converge with the image the caller obtained; the role renders it into the
# Compose project and the CLI wrapper.
converge_control_plane() {
  local image=$1
  shift
  run_playbook "${image}" control-plane.yml \
    --extra-vars "blitzecdn_controlplane_image=${image}" "$@"
}

# The image the installed services run, read back from the Compose project the
# role rendered. That line is the template's `image:`, written with `to_json`.
installed_control_plane_image() {
  [[ -f ${CONTROL_PLANE_COMPOSE_FILE} ]] || return 1
  local image
  image=$(sed -n 's/^  image: "\(.*\)"$/\1/p' "${CONTROL_PLANE_COMPOSE_FILE}" | head -n 1)
  [[ -n ${image} ]] || return 1
  printf '%s\n' "${image}"
}

# An image reference without its digest, split at the tag. The tag is the
# release version; the repository is where the next release is pulled from.
image_repository() {
  local reference=${1%%@*}
  case ${reference##*/} in
    *:*) printf '%s\n' "${reference%:*}" ;;
    *) printf '%s\n' "${reference}" ;;
  esac
}

image_version() {
  local reference=${1%%@*}
  case ${reference##*/} in
    *:*) printf '%s\n' "${reference##*:}" ;;
    *) return 1 ;;
  esac
}

# Ansible performs all system teardown, from the installed image, and must
# succeed before the installer removes the image. The image goes afterwards
# because the play runs in a container of it.
converge_uninstall() {
  local image
  image=$(installed_control_plane_image) || die 1 \
    "error: cannot uninstall: no control-plane image is recorded in ${CONTROL_PLANE_COMPOSE_FILE}" \
    "Repair the installation first, then rerun --uninstall."
  docker image inspect "${image}" >/dev/null 2>&1 || die 1 \
    "error: cannot uninstall: the control-plane image ${image} is missing" \
    "Repair the installation first, then rerun --uninstall."

  # By ID: a reference with both a tag and a digest is not one `rm` takes.
  local image_id
  image_id=$(docker image inspect --format '{{.Id}}' "${image}")

  run_playbook "${image}" uninstall.yml ||
    die 1 "error: teardown failed; inspect and repair any partially removed host state, then rerun --uninstall"
  docker image rm --force "${image_id}" >/dev/null 2>&1 || true
}

# Hand application-specific wrapper generation to the installed runtime.
handoff_user_wrapper() {
  [[ "${BLITZECDN_USER_WRAPPER:-1}" != 0 ]] || return 0
  local wrapper_dir="${BLITZECDN_WRAPPER_DIR:-${HOME:-}/.local/bin}"
  if [[ -z ${BLITZECDN_WRAPPER_DIR:-} && -z ${HOME:-} ]]; then
    echo "warning: HOME is unset; skipping the ${script_dir}/.venv/bin/blitzecdn shortcut" >&2
    return 0
  fi
  .venv/bin/python -m blitzecdn.entrypoints.install_handoff wrapper \
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
# Docker and the control-plane image — what a server needs before Ansible
# ---------------------------------------------------------------------------

# The Docker repository and daemon configuration, byte for byte what the
# blitzecdn_docker role renders from docker.sources.j2 and daemon.json.j2 with
# its defaults. They have to agree: the play runs inside a container on this
# engine, so a converge that rewrote either would restart the daemon under the
# Ansible doing it. A test renders the templates and compares.
docker_sources() {
  local id=$1 codename=$2 architecture=$3
  printf '%s\n' \
    '# Managed by BlitzeCDN. Local edits are overwritten.' \
    'Types: deb' \
    "URIs: https://download.docker.com/linux/${id}" \
    "Suites: ${codename}" \
    'Components: stable' \
    "Architectures: ${architecture}" \
    'Signed-By: /etc/apt/keyrings/docker.asc'
}

docker_daemon_json() {
  cat <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "32m",
    "max-file": "3"
  },
  "live-restore": true
}
EOF
}

# Write one root-owned 0644 file from stdin, reporting through the status
# whether its content changed. Mode and owner are converged too: the role's
# template task would otherwise report a change, and for daemon.json a change
# notifies a daemon restart.
converge_host_file() {
  local path=$1 staged
  staged=$(mktemp)
  cleanup_paths+=("${staged}")
  cat >"${staged}"
  if [[ -f ${path} ]] && cmp -s "${staged}" "${path}"; then
    chown root:root "${path}"
    chmod 0644 "${path}"
    return 1
  fi
  install -o root -g root -m 0644 "${staged}" "${path}"
}

# Install Docker Engine and Compose from Docker's own repository, exactly as
# blitzecdn_docker does, which then finds nothing to change. The engine has to
# exist before the image this installer runs everything else from.
# shellcheck disable=SC1091 # /etc/os-release is the host's, not this repository's
ensure_docker() {
  local id codename architecture
  id=$(. /etc/os-release && printf %s "${ID:-}")
  codename=$(. /etc/os-release && printf %s "${VERSION_CODENAME:-}")
  architecture=$(dpkg --print-architecture)

  install -d -o root -g root -m 0755 /etc/apt/keyrings /etc/docker
  if [[ ! -s /etc/apt/keyrings/docker.asc ]]; then
    local key
    key=$(mktemp)
    cleanup_paths+=("${key}")
    curl -fsSL --proto '=https' --tlsv1.2 \
      --retry 3 --retry-connrefused --retry-all-errors --retry-delay 2 \
      -o "${key}" "https://download.docker.com/linux/${id}/gpg" ||
      die 1 "error: could not download Docker's apt signing key"
    install -o root -g root -m 0644 "${key}" /etc/apt/keyrings/docker.asc
  fi
  # Redirected rather than piped, so the helper runs in this shell and its
  # scratch file is cleaned up with the rest.
  converge_host_file /etc/apt/sources.list.d/docker.sources \
    < <(docker_sources "${id}" "${codename}" "${architecture}") || true
  local daemon_changed=0
  if converge_host_file /etc/docker/daemon.json < <(docker_daemon_json); then
    daemon_changed=1
  fi

  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  if [[ ${daemon_changed} -eq 1 ]]; then
    systemctl restart docker
  fi
  systemctl enable --now docker
  docker info >/dev/null || die 1 "error: Docker Engine is installed but does not answer"
}

# Pull an image, retrying the way the uv download does: a registry that drops
# one connection should not fail an installation.
pull_image() {
  local image=$1 attempt
  for attempt in 1 2 3; do
    docker pull "${image}" >&2 && return 0
    [[ ${attempt} -eq 3 ]] || sleep 5
  done
  return 1
}

# Pin a reference to the digest the registry served it under, keeping the tag
# for whoever reads it. A local build or a loaded archive has no registry
# digest and is printed as given.
resolve_image_digest() {
  local image=$1 repository digest
  if [[ ${image} == *@* ]]; then
    printf '%s\n' "${image}"
    return 0
  fi
  repository=$(image_repository "${image}")
  # shellcheck disable=SC2016 # a Go template, not a shell expansion
  digest=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${image}" |
    awk -v prefix="${repository}@" 'index($0, prefix) == 1 { print substr($0, length(prefix)); exit }')
  printf '%s\n' "${image}${digest}"
}

# Make a control-plane image available to the local engine and print the
# reference to run it by. Progress goes to stderr: stdout is the return value.
#
# Pulled from the registry by default. BLITZECDN_CONTROL_PLANE_ARCHIVE loads it
# from a `docker save` archive instead, and BLITZECDN_CONTROL_PLANE_BUILD=1
# builds it from the source checkout this script is in — the fallback when no
# published image can be reached, at the price of needing Docker Hub and PyPI.
obtain_control_plane_image() {
  local image=$1
  if [[ ${BLITZECDN_CONTROL_PLANE_BUILD:-0} == 1 ]]; then
    [[ -f ${script_dir}/${CONTROL_PLANE_DOCKERFILE} ]] || die 2 \
      "error: BLITZECDN_CONTROL_PLANE_BUILD=1 builds from a source checkout;" \
      "run the install.sh inside a clone of the release instead of a lone copy"
    image=${CONTROL_PLANE_SOURCE_TAG}
    echo "Building ${image} from ${script_dir}..." >&2
    docker build --pull --file "${script_dir}/${CONTROL_PLANE_DOCKERFILE}" \
      --tag "${image}" "${script_dir}" >&2 ||
      die 1 "error: could not build the control-plane image from ${script_dir}"
    printf '%s\n' "${image}"
    return 0
  fi

  if [[ -n ${BLITZECDN_CONTROL_PLANE_ARCHIVE:-} ]]; then
    echo "Loading the control-plane image from ${BLITZECDN_CONTROL_PLANE_ARCHIVE}..." >&2
    docker load --input "${BLITZECDN_CONTROL_PLANE_ARCHIVE}" >&2 ||
      die 1 "error: could not load ${BLITZECDN_CONTROL_PLANE_ARCHIVE}"
    docker image inspect "${image}" >/dev/null 2>&1 ||
      die 1 "error: ${BLITZECDN_CONTROL_PLANE_ARCHIVE} does not contain ${image}" \
        "Set BLITZECDN_CONTROL_PLANE_IMAGE to the reference the archive holds."
  elif ! docker image inspect "${image}" >/dev/null 2>&1; then
    echo "Pulling ${image}..." >&2
    pull_image "${image}" || die 1 \
      "error: could not pull the control-plane image ${image}" \
      "Retry, or load it with BLITZECDN_CONTROL_PLANE_ARCHIVE."
  fi
  resolve_image_digest "${image}"
}

# ---------------------------------------------------------------------------
# (no arguments) — virtualenv and collections
# ---------------------------------------------------------------------------

# Build the virtualenv a controller checkout runs the CLI from. A server never
# runs this: everything there runs from the control-plane image.
bootstrap_runtime() {
  cd -- "${script_dir}"

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

  # Exactly what uv.lock describes; --frozen refuses a lockfile that does not
  # match pyproject.toml. BLITZECDN_DEV=1 adds the dev tooling, editable.
  local -a sync_flags=(--frozen --python "${python_command}")
  if [[ "${BLITZECDN_DEV:-0}" == "1" ]]; then
    "${uv}" sync "${sync_flags[@]}"
  else
    "${uv}" sync "${sync_flags[@]}" --no-dev --no-editable
  fi

  [[ -x .venv/bin/python && -x .venv/bin/ansible-playbook ]] ||
    die 1 "error: uv created an incomplete .venv; remove it and rerun"
  [[ -x .venv/bin/blitzecdn ]] ||
    die 1 "error: uv did not install the local BlitzeCDN CLI"

  # Third-party collections, inside the checkout. Exported because ansible.cfg
  # cannot name a path outside the package; deploys set the same variable.
  local collections_path=.state/collections
  export ANSIBLE_CONFIG=src/blitzecdn/assets/ansible/ansible.cfg
  export ANSIBLE_COLLECTIONS_PATH="${collections_path}"
  .venv/bin/ansible-galaxy collection install \
    -r src/blitzecdn/assets/ansible/requirements.yml -p "${collections_path}"
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
  --allowed-ips LIST  Client IPv4 addresses or CIDRs allowed to reach the API,
                      comma-separated or repeated. Omitted, the API stays on
                      loopback and is reached through an SSH tunnel.
  -h, --help         Show this help

Environment:
EOF
  usage_image_environment
  cat <<'EOF'

Everything runs from the release's published control-plane image, which this
pulls from ghcr.io; the host gets Docker Engine and no Python packages, and
needs no copy of the source. Fetch this script for the release to install:

  curl -fsSLO https://raw.githubusercontent.com/misaf/blitze-cdn-cp/vX.Y.Z/install.sh

The installation leaves that release's installer on the host as
blitzecdn-install. Re-running standalone repairs the release that is installed
and preserves the API key, inventory, and state; moving to another release is
`update`.
EOF
}

# A standalone run converges the release this script names. On a host that
# already runs another release that would be an update, or a downgrade no
# migration can undo, so it is refused.
require_same_release() {
  local image=$1 installed current wanted
  installed=$(installed_control_plane_image) || return 0
  current=$(image_version "${installed}") || return 0
  wanted=$(image_version "${image}") || return 0
  [[ ${current} == "${wanted}" || ${current} == local || ${wanted} == local ]] ||
    die 1 "error: this host runs ${current}, and this installer installs ${wanted}; nothing was changed" \
      "Move between releases with 'sudo ${INSTALLER_COMMAND##*/} update', or repair the" \
      "installed release with 'sudo ${INSTALLER_COMMAND##*/} standalone ...'."
}

cmd_standalone() {
  [[ -n ${parsed_admin_cidr} ]] || die 2 "error: --admin-cidr is required"
  [[ -n ${parsed_email} ]] || die 2 "error: --email is required"
  require_edge_platform

  local image=${BLITZECDN_CONTROL_PLANE_IMAGE:-${CONTROL_PLANE_IMAGE}}
  require_same_release "${image}"

  # The least this host needs before Ansible can run: an interpreter for the
  # play's modules, and curl and CA certificates to fetch Docker's signing key.
  # Everything else belongs to the control-plane role.
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates curl python3

  # The engine, then the image: Ansible, its collections and the application
  # all come from the image, so nothing is installed from PyPI.
  ensure_docker
  image=$(obtain_control_plane_image "${image}") || exit 1

  # Validated by the image about to act on them, before the host converges.
  check_inputs "${image}" standalone "${parsed_admin_cidr}" "${parsed_email}"
  local allowed_ips
  allowed_ips=$(allowed_ips_value "${image}")

  # The access list lands in the environment file the API and the firewall
  # convergence both read, so they agree from the first boot.
  converge_control_plane "${image}" \
    --extra-vars "blitzecdn_controlplane_acme_email=${parsed_email}" \
    --extra-vars "blitzecdn_controlplane_allowed_ips=${allowed_ips}"

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
  if [[ -n ${allowed_ips} ]]; then
    echo "The API also answers ${allowed_ips} directly on port 8000, over plain"
    echo "HTTP. Prefer the tunnel above when sending API credentials."
  fi
  echo "Status:  docker compose --file ${CONTROL_PLANE_COMPOSE_FILE} ps"
  echo "Update:  sudo ${INSTALLER_COMMAND##*/} update"
}

# ---------------------------------------------------------------------------
# update — move an installed host onto a newer release
# ---------------------------------------------------------------------------

usage_update() {
  cat <<'EOF'
Usage: sudo blitzecdn-install update [OPTIONS]

Move this installation onto the newest release of its own major line and leave
its state intact: pull the release's control-plane image, migrate state, and
recreate the Compose services on it.

There is nothing to choose. The release line comes from the installed image's
version, the target is the newest MAJOR.MINOR.PATCH image tag published in that
line, and an update never crosses a major version and never moves backwards.

With --major it crosses to the next major line instead, one line at a time: a
v6 host moves to the newest v7, never straight to v8. That is refused until the
host is on the newest release of its own line, where that line's deprecation
notices were published; run a plain update first, and check inventories,
playbooks and scripts against the target's release notes.

The database, the certificates, the inventory, and the API credentials in
/etc/blitzecdn all survive.

Options:
  --major      Cross to the next major line; takes a full backup and asks
               you to type the target version
  --yes        Do not ask for confirmation
  --no-backup  Skip the backup taken before anything changes
  --allowed-ips LIST
               Repoint the API access list while updating; comma-separated or
               repeated. Omitted, this update does not touch it.
  -h, --help   Show this help

Environment:
EOF
  usage_image_environment
  cat <<'EOF'

BLITZECDN_CONTROL_PLANE_IMAGE names the target instead of asking the registry;
it is still refused unless it moves forward within the line, or to the next
line with --major. The persistent containers are stopped while the schema and
immutable image are replaced.
EOF
}

# An update inside a line asks y/N. A major move crosses a compatibility
# boundary and wants the target typed, so the operator has read where they are
# going.
confirm_release_move() {
  local assume_yes="$1" current="$2" target="$3"
  if [[ ${parsed_major} -eq 0 ]]; then
    cat >&2 <<EOF

Updating the BlitzeCDN control plane on this host:

  from  ${current}
  to    ${target}

The containers are stopped, the schema is migrated, and they are recreated on
the new image. State is preserved; a migration is not reversible.
EOF
  else
    cat >&2 <<EOF

Moving the BlitzeCDN control plane across a major release line:

  from  ${current}
  to    ${target}

A major release is allowed to remove things this host may depend on: Ansible
variables and role names an inventory sets, CLI commands a script runs, and API
routes a client calls. What changed is in ${target}'s release notes.

The containers are stopped, a full backup is taken, the schema is migrated,
and they are recreated on the new image. A migration is not reversible: going
back means restoring the backup.
EOF
  fi
  [[ ${assume_yes} == 1 ]] && return 0
  local answer
  if [[ ${parsed_major} -eq 0 ]]; then
    read -r -p "Continue? [y/N]: " answer
    [[ ${answer} =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
  else
    read -r -p "Type ${target} to continue: " answer
    [[ ${answer} == "${target}" ]] || { echo "Cancelled."; exit 0; }
  fi
}

# Every refusal happens before the backup; the services keep running until
# the role stops them, after the target's image is in place.
cmd_update() {
  [[ ${BLITZECDN_CONTROL_PLANE_BUILD:-0} != 1 ]] || die 2 \
    "error: update moves between published releases; BLITZECDN_CONTROL_PLANE_BUILD=1 is for standalone"

  local installed current
  installed=$(installed_control_plane_image) || die 1 \
    "error: no installation found: ${CONTROL_PLANE_COMPOSE_FILE} names no control-plane image" \
    "Install this server with 'standalone' first."
  docker image inspect "${installed}" >/dev/null 2>&1 || die 1 \
    "error: the installed control-plane image ${installed} is missing; nothing was changed" \
    "Load it again, or repair with '${INSTALLER_COMMAND##*/} standalone', then rerun update."
  current=$(image_version "${installed}") || die 1 \
    "error: the installed image ${installed} names no release version; nothing was changed"

  # Empty when the flag was not given, which leaves the installed list alone.
  local allowed_ips
  allowed_ips=$(allowed_ips_value "${installed}")

  # The target, chosen by the installed release's own code: the registry's
  # tags, or the image the operator named, checked for direction either way.
  local selection=("${current}") named=${BLITZECDN_CONTROL_PLANE_IMAGE:-} target
  if [[ ${parsed_major} -eq 1 ]]; then
    selection+=(--major)
  fi
  if [[ -n ${named} ]]; then
    local named_version
    named_version=$(image_version "${named}") || die 2 \
      "error: BLITZECDN_CONTROL_PLANE_IMAGE must be tagged with its release version: ${named}"
    selection+=(--target "${named_version}")
  fi
  target=$(run_image_helper "${installed}" blitzecdn.host.image_releases "${selection[@]}") || exit 1
  if [[ ${target} == "${current}" ]]; then
    echo "Already on ${current}; nothing to update."
    return 0
  fi
  local image=${named:-$(image_repository "${installed}"):${target}}

  confirm_release_move "${parsed_yes}" "${current}" "${target}"

  # An update backs up the database, the one thing it can damage; a major may
  # move more, so it backs up everything. Through the wrapper, as the image's
  # account; VACUUM INTO is safe against a serving controller.
  local backup_label="the database backup" backup_args=(backup create --only database)
  if [[ ${parsed_major} -eq 1 ]]; then
    backup_label="the backup"
    backup_args=(backup create)
  fi
  if [[ ${parsed_no_backup} -eq 1 ]]; then
    echo "Skipping ${backup_label} (--no-backup)."
  elif [[ -x ${CLI_WRAPPER} ]]; then
    echo "Taking ${backup_label}..."
    "${CLI_WRAPPER}" "${backup_args[@]}" ||
      die 1 "error: ${backup_label} failed; nothing was changed" \
        "Fix the failure, or rerun with --no-backup if the data is expendable."
  else
    die 1 "error: ${CLI_WRAPPER} is missing, so ${backup_label} cannot be taken; nothing was changed" \
      "Repair with '${INSTALLER_COMMAND##*/} standalone', or rerun with --no-backup if the data is expendable."
  fi

  # The same steps as standalone, with the target's image. The play runs
  # inside that image, and the role stops the services, migrates, recreates
  # them on it, replaces this installer with the target's, and waits until
  # both services are healthy.
  ensure_docker
  image=$(obtain_control_plane_image "${image}") || die 1 \
    "error: the services were not stopped and still run ${current}; fix the cause and rerun"
  converge_control_plane "${image}" \
    --extra-vars "blitzecdn_controlplane_allowed_ips=${allowed_ips}"

  echo
  echo "Updated to ${target}; the API and worker are healthy."
}

# ---------------------------------------------------------------------------
# uninstall — remove every artifact
# ---------------------------------------------------------------------------

# Everything an uninstall deletes, read by the help and by the confirmation,
# so the prompt cannot understate what is about to happen.
removal_manifest() {
  cat <<'EOF'
  - the control-plane Compose project, its image, and BlitzeCDN host timers
  - /etc/blitzecdn and the API credentials it holds
  - /var/lib/blitzecdn: the database, certificates, keys, and the rest of the
    control plane's state
  - the /usr/local/bin/blitzecdn and /usr/local/sbin/blitzecdn-install commands
  - the sudo rule /etc/sudoers.d/blitzecdn-deploy
  - the deploy account and its home
  - the edge-managed nginx sites, certificates, cache, and drop-ins this host
    converged as an edge
EOF
}

usage_uninstall() {
  cat <<'EOF'
Usage: sudo blitzecdn-install --uninstall [OPTIONS]

Remove every BlitzeCDN artifact from this host and stop there, leaving the
system packages it had and nothing BlitzeCDN wrote.

Options:
  --yes        Do not ask for confirmation
  -h, --help   Show this help

Removed:
EOF
  removal_manifest
  cat <<'EOF'

Kept: the backups in /var/backups/blitzecdn. Take one first with
`sudo blitzecdn backup create` if the state matters.

Nothing outside those paths is changed and no packages are removed. The
installed control-plane image must still be present: the Ansible in it
performs all system teardown. To reinstall from a clean state afterwards,
fetch install.sh for the release you want and run its standalone command.
EOF
}

confirm_destructive() {
  local assume_yes="$1"
  [[ ${assume_yes} == 1 ]] && return 0
  echo >&2 "This removes every BlitzeCDN artifact on this host and cannot be undone:"
  echo >&2
  removal_manifest >&2
  local answer
  read -r -p "Continue? [y/N]: " answer
  [[ ${answer} =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
}

cmd_uninstall() {
  confirm_destructive "${parsed_yes}"
  converge_uninstall

  echo
  echo "BlitzeCDN has been removed. No packages were removed and nothing outside"
  echo "the paths listed in the confirmation was changed."
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
      help|-h|--help)
        usage_root
        exit 0
        ;;
    esac
  fi

  local usage root_label
  usage=$(command_field "${subcommand}" 2)
  root_label=$(command_field "${subcommand}" 3)

  # Parsing first, so `--help` answers without sudo.
  parse_options "${subcommand}" "${usage}" "$@"
  [[ -z ${root_label} ]] || require_root "${root_label}"

  "cmd_${subcommand}"
}

# Sourcing this file defines its functions and runs nothing, so tests can call
# the helpers directly.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
