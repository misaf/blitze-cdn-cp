#!/usr/bin/env bash
# BlitzeCDN installer.
#
# Three jobs live here behind subcommands, because they are the same job at
# different scopes and they were drifting apart as separate files:
#
#   (no arguments)  build the virtualenv and install the pinned collections
#   standalone      provision this whole server as control plane and edge
#   update          move an installed server to a newer release
#
# The no-argument form must keep working forever and must keep taking no
# arguments. `update` checks out the target release and then runs
# `${INSTALL_DIR}/install.sh` with no arguments, using its own pre-update copy
# of this script — so an installation from any older release upgrades by
# invoking whatever this file has become. Adding a required argument to the
# default path would strand every existing host at its current version.
set -Eeuo pipefail

readonly INSTALL_DIR=/opt/blitzecdn
readonly BACKUP_DIR=/var/backups/blitzecdn
readonly SERVICES=(
  blitzecdn-api.service
  blitzecdn-cert-renew.timer
  blitzecdn-drift.timer
)
readonly SYSTEMD_UNITS=(
  blitzecdn-api.service
  blitzecdn-cert-renew.service
  blitzecdn-cert-renew.timer
  blitzecdn-drift.service
  blitzecdn-drift.timer
)

# Captured before dispatch strips the subcommand, so `update` can hand the
# original invocation to the private copy it re-execs.
original_args=("$@")
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

usage_root() {
  cat <<'EOF'
Usage: ./install.sh [SUBCOMMAND] [OPTIONS]

Install and update BlitzeCDN.

Subcommands:
  (none)      Build .venv and install the pinned Ansible collections
  standalone  Provision this server as an independent control plane and edge
  update      Update a standalone installation to a newer release
  help        Show this message

Run './install.sh SUBCOMMAND --help' for the options of a subcommand.

Environment for the no-argument form:
  BLITZECDN_PYTHON     Python interpreter to build .venv with (default python3)
  BLITZECDN_DEV=1      Install -e '.[dev]' instead of a plain wheel
  BLITZECDN_EDGE_PATH  Build and install the edge collection from a checkout
                       instead of the release pinned in ansible/requirements.yml
EOF
}

install_systemd_units() {
  local unit
  for unit in "${SYSTEMD_UNITS[@]}"; do
    install -m 0644 "packaging/systemd/${unit}" /etc/systemd/system/
  done
}

# ---------------------------------------------------------------------------
# (no arguments) — virtualenv and collections
# ---------------------------------------------------------------------------

cmd_install() {
  [[ $# -eq 0 ]] || {
    echo "error: the default install takes no arguments: $*" >&2
    usage_root >&2
    exit 2
  }

  cd -- "${script_dir}"

  local python_command="${BLITZECDN_PYTHON:-python3}"
  if ! command -v "${python_command}" >/dev/null 2>&1; then
    echo "error: ${python_command} was not found; install Python 3.12-3.14" >&2
    exit 1
  fi

  "${python_command}" -c '
import sys
if not ((3, 12) <= sys.version_info[:2] < (3, 15)):
    raise SystemExit("error: BlitzeCDN requires Python 3.12, 3.13, or 3.14")
'

  if [[ -e .venv && (! -x .venv/bin/python || ! -x .venv/bin/pip) ]]; then
    local invalid_venv
    invalid_venv=".venv.invalid.$(date -u +%Y%m%dT%H%M%SZ)"
    echo "warning: .venv is incomplete; preserving it as ${invalid_venv}" >&2
    mv -- .venv "${invalid_venv}"
  fi

  if [[ ! -d .venv ]]; then
    "${python_command}" -m venv .venv
  fi

  if [[ ! -x .venv/bin/python || ! -x .venv/bin/pip ]]; then
    echo "error: Python created an incomplete .venv; install the Python venv package and retry" >&2
    exit 1
  fi

  # BLITZECDN_DEV=1 installs the test and lint tooling and makes src/ edits take
  # effect without reinstalling. Without it the venv holds a plain wheel, which is
  # what an operator wants and what a contributor gets caught by.
  if [[ "${BLITZECDN_DEV:-0}" == "1" ]]; then
    .venv/bin/python -m pip install -e '.[dev]'
  else
    .venv/bin/python -m pip install .
    .venv/bin/python -m pip install 'ansible-core>=2.21,<2.22'
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

  # BLITZECDN_EDGE_PATH points at a blitze-cdn-edge checkout and installs the
  # collection built from it instead of the release pinned in
  # ansible/requirements.yml. That is the only supported way to run un-released
  # edge changes — a deploy otherwise always uses the pinned tag — so it is
  # opt-in, and it prints what it did because the pin no longer describes the
  # installed roles.
  if [[ -n "${BLITZECDN_EDGE_PATH:-}" ]]; then
    local edge_path="${BLITZECDN_EDGE_PATH}"
    if [[ ! -f "${edge_path}/galaxy.yml" ]]; then
      echo "error: ${edge_path} is not a blitze-cdn-edge checkout (no galaxy.yml)" >&2
      exit 1
    fi

    local edge_version
    edge_version="$(
      sed -n 's/^version:[[:space:]]*//p' "${edge_path}/galaxy.yml" |
        tr -d "\"'" | head -n 1
    )"
    if [[ -z "${edge_version}" ]]; then
      echo "error: no version field in ${edge_path}/galaxy.yml" >&2
      exit 1
    fi

    .venv/bin/ansible-galaxy collection build "${edge_path}" \
      --output-path "${edge_path}/dist" --force
    # --no-deps keeps ansible.posix and community.general on the versions pinned
    # below rather than whatever Galaxy currently resolves the ranges in
    # galaxy.yml to. Only the edge roles are meant to be un-released here.
    .venv/bin/ansible-galaxy collection install \
      "${edge_path}/dist/blitzecdn-edge-${edge_version}.tar.gz" \
      -p "${collections_path}" --no-deps --force
    local support_collections
    support_collections="$(
      .venv/bin/python - ansible/requirements.yml <<'PY'
import sys

import yaml

with open(sys.argv[1], encoding="utf-8") as handle:
    document = yaml.safe_load(handle)

for entry in document["collections"]:
    if entry["name"] != "blitzecdn.edge":
        print(f"{entry['name']}:{entry['version']}")
PY
    )"
    # shellcheck disable=SC2086 # each word is one name:version argument
    .venv/bin/ansible-galaxy collection install ${support_collections} \
      -p "${collections_path}"
    echo "installed blitzecdn.edge ${edge_version} from ${edge_path} (not the pinned release)"
  else
    # Git-backed collection requirements use v-prefixed release tags, while the
    # installed MANIFEST version is necessarily numeric. ansible-core 2.21 tries
    # to compare those unlike representations on an existing installation and
    # raises from LooseVersion before it can update. --force deliberately skips
    # that broken comparison and makes upgrades as reliable as clean installs.
    .venv/bin/ansible-galaxy collection install \
      -r ansible/requirements.yml -p "${collections_path}" --force
  fi

  .venv/bin/blitzecdn setup

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
  --no-deploy         Deprecated compatibility alias for the safe default
  -h, --help         Show this help

The checkout must be /opt/blitzecdn because the hardened systemd units use that
path. Re-running the installer preserves the API key, inventory, and state.
EOF
}

cmd_standalone() {
  local admin_cidr='' acme_email='' run_deploy=0 allow_empty_sites=0
  local public_addresses=()

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --admin-cidr)
        [[ $# -ge 2 ]] || { echo "error: --admin-cidr needs a value" >&2; exit 2; }
        admin_cidr=$2
        shift 2
        ;;
      --email)
        [[ $# -ge 2 ]] || { echo "error: --email needs a value" >&2; exit 2; }
        acme_email=$2
        shift 2
        ;;
      --deploy)
        run_deploy=1
        shift
        ;;
      --allow-empty-sites)
        allow_empty_sites=1
        shift
        ;;
      --public-address)
        [[ $# -ge 2 ]] || { echo "error: --public-address needs a value" >&2; exit 2; }
        public_addresses+=("$2")
        shift 2
        ;;
      --no-deploy)
        run_deploy=0
        shift
        ;;
      -h|--help)
        usage_standalone
        exit 0
        ;;
      *)
        echo "error: unknown option: $1" >&2
        usage_standalone >&2
        exit 2
        ;;
    esac
  done

  [[ ${EUID} -eq 0 ]] || { echo "error: run this installer with sudo" >&2; exit 1; }
  [[ -n ${admin_cidr} ]] || { echo "error: --admin-cidr is required" >&2; exit 2; }
  [[ -n ${acme_email} ]] || { echo "error: --email is required" >&2; exit 2; }

  [[ ${script_dir} == "${INSTALL_DIR}" ]] || {
    echo "error: clone or copy this release to ${INSTALL_DIR}, then run the installer there" >&2
    exit 1
  }
  cd -- "${script_dir}"

  if [[ ! -r /etc/os-release ]]; then
    echo "error: this installer supports Debian 12+ and Ubuntu 24.04+" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source /etc/os-release
  case "${ID:-}" in
    debian|ubuntu) ;;
    *) echo "error: unsupported operating system: ${ID:-unknown}" >&2; exit 1 ;;
  esac
  python3 - "${ID}" "${VERSION_ID:-0}" <<'PY'
import sys

minimum = {"debian": 12, "ubuntu": 24}
try:
    major = int(sys.argv[2].split(".", 1)[0])
except ValueError as error:
    raise SystemExit(f"error: invalid operating-system version: {sys.argv[2]}") from error
if major < minimum[sys.argv[1]]:
    raise SystemExit(
        f"error: {sys.argv[1]} {sys.argv[2]} is unsupported; "
        f"version {minimum[sys.argv[1]]}+ is required"
    )
PY

  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y certbot git openssh-server openssl python3 python3-venv sudo
  systemctl enable --now ssh

  python3 - "${admin_cidr}" "${acme_email}" <<'PY'
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

  if ! getent passwd blitzecdn >/dev/null; then
    useradd --system --create-home --home-dir /var/lib/blitzecdn blitzecdn
  fi
  if ! getent passwd deploy >/dev/null; then
    useradd --create-home --shell /bin/bash deploy
  fi

  # useradd deliberately leaves a pre-existing home alone. A partial prior run
  # can therefore leave root-owned cache and Ansible directories that break every
  # later run as the service account. This is a dedicated service home, so make
  # its ownership invariant explicit on every installation.
  # Nginx needs traverse access to serve HTTP-01 tokens below this service home.
  # Other users still cannot list it, and child directories keep stricter modes.
  install -d -m 0751 -o blitzecdn -g blitzecdn /var/lib/blitzecdn
  local service_path
  for service_path in .ansible .cache .ssh; do
    if [[ -e "/var/lib/blitzecdn/${service_path}" ]]; then
      chown -R blitzecdn:blitzecdn "/var/lib/blitzecdn/${service_path}"
    fi
  done

  install -d -m 0750 -o blitzecdn -g blitzecdn "${INSTALL_DIR}"
  chown -R blitzecdn:blitzecdn "${INSTALL_DIR}"

  local sudoers_file=/etc/sudoers.d/blitzecdn-deploy
  printf '%s\n' 'deploy ALL=(root) NOPASSWD: ALL' > "${sudoers_file}"
  chmod 0440 "${sudoers_file}"
  visudo -cf "${sudoers_file}" >/dev/null

  runuser -u blitzecdn -- "${INSTALL_DIR}/install.sh"

  local cli_wrapper=/usr/local/bin/blitzecdn
  local cli_wrapper_tmp
  cli_wrapper_tmp=$(mktemp)
  trap 'rm -f -- "${cli_wrapper_tmp}"' EXIT
  cat > "${cli_wrapper_tmp}" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail

command=(
  env
  PATH=/opt/blitzecdn/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
  BLITZE_PROJECT_DIR=/opt/blitzecdn
  "BLITZE_ALLOW_EMPTY_SITES=${BLITZE_ALLOW_EMPTY_SITES:-false}"
  /opt/blitzecdn/.venv/bin/blitzecdn
  "$@"
)

run() {
  set -a
  if [[ -r /etc/blitzecdn/blitzecdn.env ]]; then
    # shellcheck disable=SC1091
    source /etc/blitzecdn/blitzecdn.env
  fi
  set +a
  exec "${command[@]}"
}

if [[ $(id -un) == blitzecdn ]]; then
  run
fi
exec sudo -u blitzecdn bash -c '
  set -euo pipefail
  set -a
  if [[ -r /etc/blitzecdn/blitzecdn.env ]]; then
    source /etc/blitzecdn/blitzecdn.env
  fi
  set +a
  exec env \
    PATH=/opt/blitzecdn/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    BLITZE_PROJECT_DIR=/opt/blitzecdn \
    "BLITZE_ALLOW_EMPTY_SITES=${BLITZE_ALLOW_EMPTY_SITES:-false}" \
    /opt/blitzecdn/.venv/bin/blitzecdn "$@"
' blitzecdn "$@"
WRAPPER
  install -m 0755 -o root -g root "${cli_wrapper_tmp}" "${cli_wrapper}"
  rm -f -- "${cli_wrapper_tmp}"
  trap - EXIT

  run_blitzecdn() {
    runuser -u blitzecdn -- env \
      PATH="${INSTALL_DIR}/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
      BLITZE_PROJECT_DIR="${INSTALL_DIR}" \
      BLITZE_ALLOW_EMPTY_SITES="${BLITZE_ALLOW_EMPTY_SITES:-false}" \
      "${INSTALL_DIR}/.venv/bin/blitzecdn" "$@"
  }

  local state_dir="${INSTALL_DIR}/.state"
  local key_file="${state_dir}/id_ed25519"
  install -d -m 0700 -o blitzecdn -g blitzecdn "${state_dir}"
  if [[ ! -f ${key_file} ]]; then
    runuser -u blitzecdn -- ssh-keygen -q -t ed25519 -N '' \
      -C blitzecdn-local-controller -f "${key_file}"
  fi

  local deploy_ssh=/home/deploy/.ssh
  local authorized_keys="${deploy_ssh}/authorized_keys"
  install -d -m 0700 -o deploy -g deploy "${deploy_ssh}"
  touch "${authorized_keys}"
  chown deploy:deploy "${authorized_keys}"
  chmod 0600 "${authorized_keys}"
  local public_key
  public_key=$(<"${key_file}.pub")
  if ! grep -Fqx -- "${public_key}" "${authorized_keys}"; then
    printf '%s\n' "${public_key}" >> "${authorized_keys}"
  fi

  local controller_ssh=/var/lib/blitzecdn/.ssh
  install -d -m 0700 -o blitzecdn -g blitzecdn "${controller_ssh}"
  local known_hosts="${controller_ssh}/known_hosts"
  ssh-keyscan -H localhost 127.0.0.1 2>/dev/null > "${known_hosts}.new"
  install -m 0600 -o blitzecdn -g blitzecdn "${known_hosts}.new" "${known_hosts}"
  rm -f "${known_hosts}.new"

  local ssh_config="${controller_ssh}/config"
  printf '%s\n' \
    'Host localhost 127.0.0.1' \
    '    IdentityFile /opt/blitzecdn/.state/id_ed25519' \
    '    IdentitiesOnly yes' \
    '    StrictHostKeyChecking yes' \
    '    UserKnownHostsFile /var/lib/blitzecdn/.ssh/known_hosts' \
    > "${ssh_config}"
  chown blitzecdn:blitzecdn "${ssh_config}"
  chmod 0600 "${ssh_config}"

  runuser -u blitzecdn -- ssh -o BatchMode=yes deploy@localhost sudo -n true

  if ! run_blitzecdn edge list --json \
    | grep -Eq '"name"[[:space:]]*:[[:space:]]*"edge-local"'; then
    local edge_add_args=(
      edge add edge-local
      --host localhost --user deploy --ssh-source "${admin_cidr}"
    )
    local public_address
    for public_address in "${public_addresses[@]}"; do
      edge_add_args+=(--public-address "${public_address}")
    done
    run_blitzecdn "${edge_add_args[@]}"
  elif [[ ${#public_addresses[@]} -gt 0 ]]; then
    local edge_update_args=(edge update edge-local)
    local public_address
    for public_address in "${public_addresses[@]}"; do
      edge_update_args+=(--public-address "${public_address}")
    done
    run_blitzecdn "${edge_update_args[@]}"
  fi

  local config_dir=/etc/blitzecdn
  local environment_file="${config_dir}/blitzecdn.env"
  install -d -m 0750 -o root -g blitzecdn "${config_dir}"
  local generated_api_key=
  if [[ ! -f ${environment_file} ]]; then
    generated_api_key=$(openssl rand -hex 32)
    umask 0077
    printf '%s\n' \
      "BLITZE_API_KEYS=operator:${generated_api_key}" \
      "BLITZE_ACME_DEFAULT_EMAIL=${acme_email}" \
      'BLITZE_INVENTORY=/opt/blitzecdn/ansible/inventory/hosts.yml' \
      'BLITZE_CERTIFICATE_RECONCILE_INTERVAL_SECONDS=600' \
      > "${environment_file}"
    chown root:blitzecdn "${environment_file}"
    chmod 0640 "${environment_file}"
  else
    local environment_tmp
    environment_tmp=$(mktemp)
    awk '!/^BLITZE_ACME_DEFAULT_EMAIL=/' "${environment_file}" > "${environment_tmp}"
    printf '%s\n' "BLITZE_ACME_DEFAULT_EMAIL=${acme_email}" >> "${environment_tmp}"
    install -m 0640 -o root -g blitzecdn "${environment_tmp}" "${environment_file}"
    rm -f -- "${environment_tmp}"
    echo "Keeping existing ${environment_file}; API credentials were not changed."
  fi

  install_systemd_units
  systemctl daemon-reload
  systemctl enable --now "${SERVICES[@]}"

  if [[ ${run_deploy} -eq 1 ]]; then
    local managed_registry=/etc/nginx/blitzecdn-managed-sites
    if [[ -s ${managed_registry} ]] && [[ $(run_blitzecdn site list) == "[]" ]] \
      && [[ ${allow_empty_sites} -ne 1 ]]; then
      echo "error: this edge has managed sites but desired state is empty" >&2
      echo "Recreate the desired sites first, or rerun with --deploy --allow-empty-sites to remove them." >&2
      exit 1
    fi
    if [[ ${allow_empty_sites} -eq 1 ]]; then
      BLITZE_ALLOW_EMPTY_SITES=true run_blitzecdn deploy --yes
    else
      run_blitzecdn deploy --yes
    fi
  else
    echo "Initial deployment skipped (safe default). Run: blitzecdn deploy"
  fi

  echo
  echo "Standalone BlitzeCDN installation complete."
  if [[ -n ${generated_api_key} ]]; then
    echo "API credential saved to ${environment_file}; read it with sudo."
  fi
  echo "Connect from your workstation with your existing operator account and key:"
  echo "  ssh -L 8000:127.0.0.1:8000 OPERATOR@THIS_SERVER"
  echo "API:     http://127.0.0.1:8000"
  echo "Status:  systemctl status blitzecdn-api.service"
}

# ---------------------------------------------------------------------------
# update — move an installed server to a newer release
# ---------------------------------------------------------------------------

usage_update() {
  cat <<'EOF'
Usage: sudo /opt/blitzecdn/install.sh update [OPTIONS]

Update a standalone BlitzeCDN installation to the newest stable release.

Options:
  --version vX.Y.Z  Install this exact release instead of the newest release
  --check           Show the installed and available versions without updating
  --yes             Do not ask for confirmation
  -h, --help        Show this help

The updater backs up persistent state and /etc/blitzecdn, updates only from an
exact v-prefixed release tag, preserves desired state and credentials, restarts
the services, and runs `blitzecdn doctor`. It never runs `blitzecdn deploy`.
EOF
}

cmd_update() {
  local requested_version='' assume_yes=0 check_only=0

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --version)
        [[ $# -ge 2 ]] || { echo "error: --version needs a value" >&2; exit 2; }
        requested_version=$2
        shift 2
        ;;
      --check)
        check_only=1
        shift
        ;;
      --yes)
        assume_yes=1
        shift
        ;;
      -h|--help)
        usage_update
        exit 0
        ;;
      *)
        echo "error: unknown option: $1" >&2
        usage_update >&2
        exit 2
        ;;
    esac
  done

  [[ ${EUID} -eq 0 ]] || { echo "error: run this updater with sudo" >&2; exit 1; }
  [[ -d ${INSTALL_DIR}/.git ]] || {
    echo "error: ${INSTALL_DIR} is not a Git checkout" >&2
    exit 1
  }

  # Continue from a private copy. Checking out a new release replaces this file,
  # and a shell must never execute a script while another process rewrites it.
  if [[ ${BLITZECDN_UPDATE_REEXEC:-0} != 1 ]]; then
    local updater_copy
    updater_copy=$(mktemp /tmp/blitzecdn-update.XXXXXX)
    install -m 0700 -- "$0" "${updater_copy}"
    exec env BLITZECDN_UPDATE_REEXEC=1 "${updater_copy}" "${original_args[@]}"
  fi
  trap 'rm -f -- "$0"' EXIT

  cd -- "${INSTALL_DIR}"

  repo_git() {
    git -c safe.directory="${INSTALL_DIR}" "$@"
  }

  if [[ -n $(repo_git status --porcelain --untracked-files=no) ]]; then
    echo "error: tracked files in ${INSTALL_DIR} have local changes" >&2
    echo "Commit or restore them before updating; persistent .state files are ignored." >&2
    exit 1
  fi

  local remote_url
  remote_url=$(repo_git remote get-url origin)
  case "${remote_url}" in
    https://github.com/misaf/blitze-cdn-cp|https://github.com/misaf/blitze-cdn-cp.git|\
    git@github.com:misaf/blitze-cdn-cp.git) ;;
    *)
      echo "error: refusing unexpected origin URL: ${remote_url}" >&2
      exit 1
      ;;
  esac

  local remote_versions
  mapfile -t remote_versions < <(
    repo_git ls-remote --tags --refs origin 'v*' |
      sed -n 's#.*refs/tags/\(v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$#\1#p' |
      sort -V
  )
  [[ ${#remote_versions[@]} -gt 0 ]] || {
    echo "error: origin has no stable vX.Y.Z release tags" >&2
    exit 1
  }

  local latest_version=${remote_versions[-1]}
  local target_version=${requested_version:-${latest_version}}
  [[ ${target_version} =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "error: version must use the vX.Y.Z format" >&2
    exit 2
  }
  # Membership in a space-delimited list, matched as a glob rather than a
  # regex: a quoted =~ right-hand side already matches literally, which reads
  # as a regex that silently is not one.
  if [[ " ${remote_versions[*]} " != *" ${target_version} "* ]]; then
    echo "error: ${target_version} is not a stable release tag on origin" >&2
    exit 1
  fi

  local current_version
  current_version=$(python3 - <<'PY'
import pathlib
import tomllib

document = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
print(f"v{document['project']['version']}")
PY
  )

  echo "Installed: ${current_version}"
  echo "Latest:    ${latest_version}"
  echo "Target:    ${target_version}"

  if [[ ${check_only} -eq 1 ]]; then
    exit 0
  fi
  if [[ ${current_version} == "${target_version}" ]]; then
    echo "BlitzeCDN is already up to date."
    exit 0
  fi

  if [[ ${assume_yes} -ne 1 ]]; then
    local answer
    read -r -p "Update BlitzeCDN to ${target_version}? [y/N]: " answer
    [[ ${answer} =~ ^[Yy]$ ]] || { echo "Update cancelled."; exit 0; }
  fi

  local previous_commit timestamp backup_path
  previous_commit=$(repo_git rev-parse HEAD)
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  backup_path=${BACKUP_DIR}/${timestamp}-${current_version}.tar.gz

  # shellcheck disable=SC2329 # invoked indirectly by 'trap rollback ERR'
  rollback() {
    local exit_code=$?
    trap - ERR
    echo "error: update failed; restoring the previous checkout" >&2
    repo_git checkout --detach "${previous_commit}" >&2 || true
    chown -R blitzecdn:blitzecdn "${INSTALL_DIR}" >&2 || true
    runuser -u blitzecdn -- "${INSTALL_DIR}/install.sh" >&2 || true
    install_systemd_units >&2 || true
    systemctl daemon-reload >&2 || true
    systemctl start "${SERVICES[@]}" >&2 || true
    echo "State backup: ${backup_path}" >&2
    exit "${exit_code}"
  }

  repo_git fetch --no-tags origin \
    "refs/tags/${target_version}:refs/tags/${target_version}"
  local tagged_version
  tagged_version=$(repo_git show "${target_version}:pyproject.toml" | python3 -c '
import sys
import tomllib

document = tomllib.loads(sys.stdin.read())
print("v" + document["project"]["version"])
')
  [[ ${tagged_version} == "${target_version}" ]] || {
    echo "error: ${target_version} contains package version ${tagged_version}" >&2
    false
  }

  trap rollback ERR
  systemctl stop "${SERVICES[@]}"
  mkdir -p -- "${BACKUP_DIR}"
  local backup_items=(opt/blitzecdn/.state)
  [[ -f .env ]] && backup_items+=(opt/blitzecdn/.env)
  [[ -d /etc/blitzecdn ]] && backup_items+=(etc/blitzecdn)
  tar -czf "${backup_path}" -C / -- "${backup_items[@]}"
  chmod 0600 "${backup_path}"
  echo "Backup:    ${backup_path}"

  repo_git checkout --detach "${target_version}"
  chown -R blitzecdn:blitzecdn "${INSTALL_DIR}"
  runuser -u blitzecdn -- "${INSTALL_DIR}/install.sh"

  install_systemd_units
  systemctl daemon-reload
  systemctl enable --now "${SERVICES[@]}"
  /usr/local/bin/blitzecdn doctor

  trap - ERR
  echo
  echo "Updated BlitzeCDN from ${current_version} to ${target_version}."
  echo "No edge configuration was deployed. Review with: blitzecdn plan"
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
    help|-h|--help)
      usage_root
      exit 0
      ;;
  esac
fi

"cmd_${subcommand}" "$@"
