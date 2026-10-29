#!/usr/bin/env bash
# BlitzeCDN installer: the bootstrap that gets a host to where Ansible and the
# stdlib helpers under src/blitzecdn/host/ can do the rest.
#
#   standalone      provision this whole server as control plane and edge, from
#                   the published control-plane image: no Python package is
#                   installed on the host, and nothing is fetched from PyPI
#   update/upgrade  move an installed host onto a newer release
#   --uninstall     remove every BlitzeCDN artifact
#   --fresh         uninstall, then reinstall the running release
set -Eeuo pipefail

readonly INSTALL_DIR=/opt/blitzecdn
readonly CONFIG_DIR=/etc/blitzecdn
readonly CLI_WRAPPER=/usr/local/bin/blitzecdn
readonly CONTROL_PLANE_COMPOSE_FILE=/etc/blitzecdn/control-plane.compose.yml
# Relative to the checkout. The role default is the one place the release pins
# its image, and is read at run time so an update pulls the release it has just
# checked out rather than the one this script came from.
readonly CONTROL_PLANE_IMAGE_PIN=src/blitzecdn/assets/ansible/roles/blitzecdn_controlplane/defaults/main.yml
# `blitzecdn.assets.docker.CONTROL_PLANE_DOCKERFILE`, for a source build.
readonly CONTROL_PLANE_DOCKERFILE=src/blitzecdn/assets/docker/control-plane/Dockerfile
readonly CONTROL_PLANE_SOURCE_TAG=blitzecdn-control-plane:local
# Where the image carries the checkout it was built from, playbooks included.
readonly IMAGE_ANSIBLE_DIR=/opt/blitzecdn/src/blitzecdn/assets/ansible
# Captured before dispatch, so the removal paths can re-exec a private copy.
original_args=("$@")
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

usage_root() {
  cat <<'EOF'
Usage: ./install.sh SUBCOMMAND [OPTIONS]

Manage a BlitzeCDN server.

Subcommands:
  standalone  Provision this server as an independent control plane and edge
  update      Move an installed server onto a newer release, keeping its state
  upgrade     Cross to the next major release line, keeping its state
  help        Show this message

Whole-host operations:
  --uninstall Remove every BlitzeCDN artifact and stop there
  --fresh     Uninstall, then reinstall like a brand-new server

Run './install.sh SUBCOMMAND --help' for the options of a subcommand.

For local development, install Python 3.12+, uv and just, then run:
  just install
  uv run blitzecdn setup
See README.md for the local development workflow.

Environment for standalone, update and upgrade:
EOF
  usage_image_environment
}

# Shared by every help that provisions from the control-plane image.
usage_image_environment() {
  cat <<'EOF'
  BLITZECDN_CONTROL_PLANE_IMAGE
                       Run this image instead of the one the release pins, for
                       example the pin with @sha256:DIGEST added, or a mirror
  BLITZECDN_CONTROL_PLANE_ARCHIVE
                       Load the image from this `docker save` archive instead
                       of pulling it, for a host without registry access
  BLITZECDN_CONTROL_PLANE_BUILD=1
                       Build the image from this checkout instead of pulling
                       it; the build itself needs Docker Hub and PyPI
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

# mktemp, registered for cleanup. Only for callers in this shell: a `$(...)`
# caller runs in a subshell the registration does not survive.
temp_path() {
  local path
  path=$(mktemp "$@")
  cleanup_paths+=("${path}")
  printf '%s\n' "${path}"
}

# Stop treating a path as this run's to delete, once it becomes the real thing.
unregister_path() {
  local keep=$1 path remaining=()
  for path in ${cleanup_paths[@]+"${cleanup_paths[@]}"}; do
    [[ ${path} == "${keep}" ]] || remaining+=("${path}")
  done
  cleanup_paths=(${remaining[@]+"${remaining[@]}"})
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
parsed_public_addresses=()
parsed_allowed_ips=()
parsed_forward_args=()

# Every subcommand: name, usage function, the sudo label (empty when root is
# not needed), and the options it takes.
readonly COMMAND_TABLE='
standalone|usage_standalone|installer|--admin-cidr --email --deploy --allow-empty-sites --public-address --allowed-ips
update|usage_update|updater|--yes --no-backup --allowed-ips
upgrade|usage_upgrade|updater|--yes --no-backup --allowed-ips
uninstall|usage_uninstall|uninstaller|--yes
fresh|usage_fresh|installer|--admin-cidr --email --deploy --allow-empty-sites --public-address --allowed-ips --yes
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
  while [[ $# -gt 0 ]]; do
    local option=$1
    case "${option}" in
      -h|--help)
        "${usage}"
        exit 0
        ;;
      --yes|--deploy|--allow-empty-sites|--no-backup)
        option_allowed "${command}" "${option}" || reject_option "${option}" "${usage}"
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
      --admin-cidr|--email|--public-address|--allowed-ips)
        option_allowed "${command}" "${option}" || reject_option "${option}" "${usage}"
        [[ $# -ge 2 ]] || die 2 "error: ${option} needs a value"
        case "${option}" in
          --admin-cidr) parsed_admin_cidr=$2 ;;
          --email) parsed_email=$2 ;;
          --public-address) parsed_public_addresses+=("$2") ;;
          --allowed-ips) parsed_allowed_ips+=("$2") ;;
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

# Re-exec this script from a private copy in /tmp, once. The removal paths
# delete the directory this file lives in, and a shell reads its script lazily.
reexec_from_private_copy() {
  local guard=$1 label=$2
  if [[ ${!guard:-0} == 1 ]]; then
    # Registered rather than trapped, so the shared EXIT handler stays.
    cleanup_paths+=("$0")
    return 0
  fi
  local copy
  copy=$(mktemp "/tmp/blitzecdn-${label}.XXXXXX")
  install -m 0700 -- "$0" "${copy}"
  exec env "${guard}=1" "${copy}" "${original_args[@]}"
}

# Git against the installation checkout, which root does not own.
repo_git() {
  git -c safe.directory="${INSTALL_DIR}" -C "${INSTALL_DIR}" "$@"
}

# Validate operator input with the stdlib helper, run by the host's own
# interpreter before the virtualenv exists.
check_inputs() {
  python3 -I "${script_dir}/src/blitzecdn/host/install_inputs.py" "$@" || exit 2
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
# only Ubuntu 26.04, and a standalone host is both. `update` and `upgrade`
# converge the control plane alone, so they do not call this.
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
    "For a controller checkout, follow README.md: just install, then uv run blitzecdn setup; register a separate Ubuntu 26.04 edge."
}

# Validate `--allowed-ips` and print the single comma-separated value the role
# takes; empty when the flag was not given. Called before anything changes.
allowed_ips_value() {
  local joined='' entry
  for entry in ${parsed_allowed_ips[@]+"${parsed_allowed_ips[@]}"}; do
    joined="${joined:+${joined},}${entry}"
  done
  if [[ -n ${joined} ]]; then
    check_inputs allowed-ips "${joined}"
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

# Ansible performs all system teardown, from the installed image, and must
# succeed before the installer removes the image and the checkout. The image
# goes afterwards because the play runs in a container of it.
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

  # Returned rather than aborting under `set -e`: both callers test it.
  local status=0
  run_playbook "${image}" uninstall.yml || status=$?
  if [[ ${status} -eq 0 ]]; then
    docker image rm --force "${image_id}" >/dev/null 2>&1 || true
  fi
  return "${status}"
}

remove_installation_directory() {
  rm -rf -- "${INSTALL_DIR}"
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

# The reference this checkout's release pins.
pinned_control_plane_image() {
  local pin="${script_dir}/${CONTROL_PLANE_IMAGE_PIN}" image
  image=$(sed -n 's/^blitzecdn_controlplane_image: *//p' "${pin}" | head -n 1)
  [[ -n ${image} ]] || die 1 "error: no control-plane image is pinned in ${pin}"
  printf '%s\n' "${image}"
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
  repository=${image}
  case ${image##*/} in
    *:*) repository=${image%:*} ;;
  esac
  # shellcheck disable=SC2016 # a Go template, not a shell expansion
  digest=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${image}" |
    awk -v prefix="${repository}@" 'index($0, prefix) == 1 { print substr($0, length(prefix)); exit }')
  printf '%s\n' "${image}${digest}"
}

# Make the control-plane image this release runs available to the local engine
# and print the reference to run it by. Progress goes to stderr: stdout is the
# return value.
#
# Pulled from the registry by default. BLITZECDN_CONTROL_PLANE_ARCHIVE loads it
# from a `docker save` archive instead, and BLITZECDN_CONTROL_PLANE_BUILD=1
# builds it from this checkout — the fallback when no published image can be
# reached, at the price of needing Docker Hub and PyPI for the build.
obtain_control_plane_image() {
  local image
  if [[ ${BLITZECDN_CONTROL_PLANE_BUILD:-0} == 1 ]]; then
    image=${CONTROL_PLANE_SOURCE_TAG}
    echo "Building ${image} from ${script_dir}..." >&2
    docker build --pull --file "${script_dir}/${CONTROL_PLANE_DOCKERFILE}" \
      --tag "${image}" "${script_dir}" >&2 ||
      die 1 "error: could not build the control-plane image from ${script_dir}"
    printf '%s\n' "${image}"
    return 0
  fi

  image=${BLITZECDN_CONTROL_PLANE_IMAGE:-}
  if [[ -z ${image} ]]; then
    image=$(pinned_control_plane_image) || exit 1
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
      "Retry, load it with BLITZECDN_CONTROL_PLANE_ARCHIVE, or build it from this" \
      "checkout with BLITZECDN_CONTROL_PLANE_BUILD=1."
  fi
  resolve_image_digest "${image}"
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
pulls from ghcr.io; the host gets Docker Engine and no Python packages. The
checkout must be /opt/blitzecdn, where `update` moves it between releases.
Re-running the installer preserves the API key, inventory, and state.
EOF
}

cmd_standalone() {
  [[ -n ${parsed_admin_cidr} ]] || die 2 "error: --admin-cidr is required"
  [[ -n ${parsed_email} ]] || die 2 "error: --email is required"
  require_edge_platform

  [[ ${script_dir} == "${INSTALL_DIR}" ]] ||
    die 1 "error: clone or copy this release to ${INSTALL_DIR}, then run the installer there"
  cd -- "${script_dir}"

  # The least this host needs before Ansible can run: an interpreter for the
  # input checks and for the play's modules, curl and CA certificates to fetch
  # Docker's signing key, and git for the release checkout. Everything else
  # belongs to the control-plane role.
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates curl git python3

  # Validated as soon as there is an interpreter, before the host converges.
  check_inputs standalone "${parsed_admin_cidr}" "${parsed_email}"
  local allowed_ips
  allowed_ips=$(allowed_ips_value)

  # The engine, then the image: Ansible, its collections and the application
  # all come from the image, so nothing is installed from PyPI.
  ensure_docker
  local image
  image=$(obtain_control_plane_image) || exit 1

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
}

# ---------------------------------------------------------------------------
# update / upgrade — move an installed host onto a newer release
# ---------------------------------------------------------------------------

usage_update() {
  cat <<'EOF'
Usage: sudo ./install.sh update [OPTIONS]

Move this installation onto the newest release of its own major line and leave
its state intact: fetch the code, pull the release's control-plane image,
migrate state, and recreate the Compose services on the new release.

There is nothing to choose. The release line comes from the installed version,
the target is the newest vMAJOR.MINOR.PATCH tag in that line, and the update
never crosses a major version and never moves backwards. It leaves the checkout
detached at that tag, so a later --fresh reinstalls the exact release that was
running. Crossing to the next major is `./install.sh upgrade`.

The database, the certificates, the inventory, and the API credentials in
/etc/blitzecdn all survive; --fresh is the destructive counterpart.

Options:
  --yes        Do not ask for confirmation
  --no-backup  Skip the database backup taken before anything changes
  --allowed-ips LIST
               Repoint the API access list while updating; comma-separated or
               repeated. Omitted, this update does not touch it.
  -h, --help   Show this help

Environment:
EOF
  usage_image_environment
  cat <<'EOF'

The checkout must be /opt/blitzecdn, must have the upstream origin, and must
have no local modifications. The persistent containers are stopped while the
schema and immutable image are replaced.
EOF
}

usage_upgrade() {
  cat <<'EOF'
Usage: ./install.sh upgrade [OPTIONS]

Cross this server onto the next major release line, one line at a time: a v3
host upgrades to the newest v4, never straight to v5.

It refuses unless the host is already on the newest release of its own line,
where that line's deprecation notices were published. Run `update` first, and
check inventories, playbooks and scripts against the target's release notes.

Everything `update` preserves, this preserves. It takes a full backup rather
than the database alone, and asks you to type the target version.

Options:
  --yes        Do not ask for confirmation
  --no-backup  Skip the backup taken before anything changes
  --allowed-ips LIST
               Repoint the API access list while upgrading; comma-separated or
               repeated. Omitted, this upgrade does not touch it.

Environment:
EOF
  usage_image_environment
}

# Check the checkout, fetch, and choose the target with `install_release.py`.
# Prints the plan as key=value lines; status 1 is a refusal it has already
# explained, and 3 an update with nothing to do.
release_plan() {
  python3 -I "${INSTALL_DIR}/src/blitzecdn/host/install_release.py" "$1" \
    --repository "${INSTALL_DIR}"
}

# Print one value of a key=value plan.
plan_field() {
  local key=$1 plan=$2 line
  while IFS= read -r line; do
    if [[ ${line%%=*} == "${key}" ]]; then
      printf '%s\n' "${line#*=}"
      return 0
    fi
  done <<<"${plan}"
  return 1
}

# An update inside a line asks y/N. An upgrade crosses a compatibility boundary
# and wants the target typed, so the operator has read where they are going.
confirm_release_move() {
  local mode="$1" assume_yes="$2" current="$3" target="$4" commits="$5"
  if [[ ${mode} == update ]]; then
    cat >&2 <<EOF

Updating the BlitzeCDN control plane on this host:

  from  ${current}
  to    ${target}   (${commits} commits)

The containers are stopped, the schema is migrated, and they are recreated on
the new image. State is preserved; a migration is not reversible.
EOF
  else
    cat >&2 <<EOF

Upgrading the BlitzeCDN control plane across a major release line:

  from  ${current}
  to    ${target}   (${commits} commits)

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
  if [[ ${mode} == update ]]; then
    read -r -p "Continue? [y/N]: " answer
    [[ ${answer} =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
  else
    read -r -p "Type ${target} to continue: " answer
    [[ ${answer} == "${target}" ]] || { echo "Cancelled."; exit 0; }
  fi
}

# The shared body of `update` and `upgrade`. Every refusal happens before the
# backup; the services keep running until the role stops them, after the
# target's image is in place.
move_release() {
  local mode=$1
  [[ ${script_dir} == "${INSTALL_DIR}" ]] ||
    die 1 "error: run the ${mode}r from the installation at ${INSTALL_DIR}"
  [[ -d ${INSTALL_DIR}/.git ]] || die 1 \
    "error: ${INSTALL_DIR} is not a Git checkout; there is nothing to ${mode} from" \
    "Clone the release to ${INSTALL_DIR} and run 'standalone' instead."
  cd -- "${script_dir}"

  # Empty when the flag was not given, which leaves the installed list alone.
  local allowed_ips
  allowed_ips=$(allowed_ips_value)

  local plan status=0 target current commits
  plan=$(release_plan "${mode}") || status=$?
  if [[ ${status} -eq 3 ]]; then
    printf '%s\n' "${plan}"
    return 0
  fi
  [[ ${status} -eq 0 ]] || exit 1
  target=$(plan_field target "${plan}")
  current=$(plan_field current "${plan}")
  commits=$(plan_field commits "${plan}")

  confirm_release_move "${mode}" "${parsed_yes}" "${current}" "${target}" "${commits}"

  # An update backs up the database, the one thing it can damage; a major may
  # move more, so an upgrade backs up everything. Through the wrapper, as the
  # image's account; VACUUM INTO is safe against a serving controller.
  local backup_label="the database backup" backup_args=(backup create --only database)
  if [[ ${mode} == upgrade ]]; then
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
    echo "warning: ${CLI_WRAPPER} is missing; skipping ${backup_label}" >&2
  fi

  # Detached at the tag: never a merge, pull, reset or force.
  echo "Checking out ${target}..."
  repo_git checkout --quiet "${target}" ||
    die 1 "error: could not check out ${target}; the services were not stopped —" \
      "restore with 'git -C ${INSTALL_DIR} checkout ${current}' and rerun"

  # The same steps as standalone, with the target's own pin: the checkout now
  # names the image to pull. The play runs inside that image, and the role
  # stops the services, migrates, recreates them on it and waits until both
  # are healthy.
  ensure_docker
  local image
  image=$(obtain_control_plane_image) || die 1 \
    "error: the services were not stopped and still run ${current};" \
    "fix the cause and rerun, or restore with 'git -C ${INSTALL_DIR} checkout ${current}'"
  converge_control_plane "${image}" \
    --extra-vars "blitzecdn_controlplane_allowed_ips=${allowed_ips}"

  echo
  if [[ ${mode} == update ]]; then
    echo "Updated to ${target}; the API and worker are healthy."
  else
    echo "Upgraded to ${target}; the API and worker are healthy."
  fi
}

cmd_update() {
  move_release update
}

cmd_upgrade() {
  move_release upgrade
}

# ---------------------------------------------------------------------------
# uninstall / fresh — remove every artifact, or wipe and rebuild clean
# ---------------------------------------------------------------------------

# Everything the two removal paths delete, read by the help and by the
# confirmation, so the prompt cannot understate what is about to happen.
removal_manifest() {
  cat <<'EOF'
  - the installation directory /opt/blitzecdn and everything in it: source,
    .venv, .env, and .state (database, certificates, collections, locks, logs)
  - the control-plane Compose project, its image, and BlitzeCDN host timers
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

Nothing outside those paths is changed and no packages are removed. The
installed control-plane image must still be present: the Ansible in it
performs all system teardown. The installer then removes the image and its own
checkout from a private copy.
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
  --allowed-ips LIST Client IPv4 addresses or CIDRs allowed to reach the API
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
  reexec_from_private_copy BLITZECDN_UNINSTALL_REEXEC uninstall

  confirm_destructive "${parsed_yes}" uninstall
  converge_uninstall
  remove_installation_directory

  echo
  echo "BlitzeCDN has been removed. No packages were removed and nothing outside"
  echo "the paths listed in the confirmation was changed."
}

cmd_fresh() {
  reexec_from_private_copy BLITZECDN_UNINSTALL_REEXEC fresh

  [[ -d ${INSTALL_DIR}/.git ]] || die 1 \
    "error: ${INSTALL_DIR} is not a Git checkout; nothing to reinstall from" \
    "Run --uninstall instead, or clone the release to ${INSTALL_DIR} first."

  # The origin and the running release, read before the teardown deletes the
  # checkout: a tag or the release branch by name, anything else by commit.
  local source remote_url revision named_revision
  source=$(release_plan source) || exit 1
  remote_url=$(plan_field origin "${source}")
  revision=$(plan_field revision "${source}")
  named_revision=$(plan_field named "${source}")

  # Before the confirmation: a rebuild that cannot reinstall is a deletion.
  require_edge_platform

  confirm_destructive "${parsed_yes}" fresh

  # Stage the replacement before destroying anything, beside the final path so
  # the handoff is a rename. Registered, so every refusal removes it.
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
  # From here the staged clone is the only copy of the release.
  unregister_path "${staging}"
  mv -- "${staging}" "${INSTALL_DIR}" ||
    die 1 "error: teardown succeeded but the staged release remains at ${staging}; move it to ${INSTALL_DIR} and rerun standalone"

  echo
  echo "Reinstalling the running release from a clean state..."
  "${INSTALL_DIR}/install.sh" standalone \
    ${parsed_forward_args[@]+"${parsed_forward_args[@]}"}
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

main() {
  if [[ $# -eq 0 ]]; then
    usage_root
    return 0
  fi
  local subcommand
  case "$1" in
    standalone|update|upgrade)
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
    *)
      echo "error: unknown subcommand: $1" >&2
      usage_root >&2
      return 2
      ;;
  esac

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
