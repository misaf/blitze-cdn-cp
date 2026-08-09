#!/usr/bin/env bash
set -Eeuo pipefail

install_dir=/opt/blitzecdn
backup_dir=/var/backups/blitzecdn
requested_version=
assume_yes=0
check_only=0
original_args=("$@")

usage() {
  cat <<'EOF'
Usage: sudo /opt/blitzecdn/update-standalone.sh [OPTIONS]

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
      usage
      exit 0
      ;;
    *)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ ${EUID} -eq 0 ]] || { echo "error: run this updater with sudo" >&2; exit 1; }
[[ -d ${install_dir}/.git ]] || {
  echo "error: ${install_dir} is not a Git checkout" >&2
  exit 1
}

# Continue from a private copy. Checking out a new release replaces this file,
# and a shell must never execute a script while another process rewrites it.
if [[ ${BLITZECDN_UPDATE_REEXEC:-0} != 1 ]]; then
  updater_copy=$(mktemp /tmp/blitzecdn-update.XXXXXX)
  install -m 0700 -- "$0" "${updater_copy}"
  exec env BLITZECDN_UPDATE_REEXEC=1 "${updater_copy}" "${original_args[@]}"
fi
trap 'rm -f -- "$0"' EXIT

cd -- "${install_dir}"

repo_git() {
  git -c safe.directory="${install_dir}" "$@"
}

if [[ -n $(repo_git status --porcelain --untracked-files=no) ]]; then
  echo "error: tracked files in ${install_dir} have local changes" >&2
  echo "Commit or restore them before updating; persistent .state files are ignored." >&2
  exit 1
fi

remote_url=$(repo_git remote get-url origin)
case "${remote_url}" in
  https://github.com/misaf/blitze-cdn-cp|https://github.com/misaf/blitze-cdn-cp.git|\
  git@github.com:misaf/blitze-cdn-cp.git) ;;
  *)
    echo "error: refusing unexpected origin URL: ${remote_url}" >&2
    exit 1
    ;;
esac

mapfile -t remote_versions < <(
  repo_git ls-remote --tags --refs origin 'v*' |
    sed -n 's#.*refs/tags/\(v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$#\1#p' |
    sort -V
)
[[ ${#remote_versions[@]} -gt 0 ]] || {
  echo "error: origin has no stable vX.Y.Z release tags" >&2
  exit 1
}

latest_version=${remote_versions[-1]}
target_version=${requested_version:-${latest_version}}
[[ ${target_version} =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "error: version must use the vX.Y.Z format" >&2
  exit 2
}
if [[ ! " ${remote_versions[*]} " =~ " ${target_version} " ]]; then
  echo "error: ${target_version} is not a stable release tag on origin" >&2
  exit 1
fi

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
  read -r -p "Update BlitzeCDN to ${target_version}? [y/N]: " answer
  [[ ${answer} =~ ^[Yy]$ ]] || { echo "Update cancelled."; exit 0; }
fi

previous_commit=$(repo_git rev-parse HEAD)
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_path=${backup_dir}/${timestamp}-${current_version}.tar.gz
services=(
  blitzecdn-api.service
  blitzecdn-cert-renew.timer
  blitzecdn-drift.timer
)

rollback() {
  exit_code=$?
  trap - ERR
  echo "error: update failed; restoring the previous checkout" >&2
  repo_git checkout --detach "${previous_commit}" >&2 || true
  chown -R blitzecdn:blitzecdn "${install_dir}" >&2 || true
  runuser -u blitzecdn -- "${install_dir}/install.sh" >&2 || true
  install -m 0644 packaging/systemd/blitzecdn-api.service \
    /etc/systemd/system/ >&2 || true
  install -m 0644 packaging/systemd/blitzecdn-cert-renew.service \
    /etc/systemd/system/ >&2 || true
  install -m 0644 packaging/systemd/blitzecdn-cert-renew.timer \
    /etc/systemd/system/ >&2 || true
  install -m 0644 packaging/systemd/blitzecdn-drift.service \
    /etc/systemd/system/ >&2 || true
  install -m 0644 packaging/systemd/blitzecdn-drift.timer \
    /etc/systemd/system/ >&2 || true
  systemctl daemon-reload >&2 || true
  systemctl start "${services[@]}" >&2 || true
  echo "State backup: ${backup_path}" >&2
  exit "${exit_code}"
}

repo_git fetch --no-tags origin \
  "refs/tags/${target_version}:refs/tags/${target_version}"
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
systemctl stop "${services[@]}"
mkdir -p -- "${backup_dir}"
backup_items=(opt/blitzecdn/.state)
[[ -f .env ]] && backup_items+=(opt/blitzecdn/.env)
[[ -d /etc/blitzecdn ]] && backup_items+=(etc/blitzecdn)
tar -czf "${backup_path}" -C / -- "${backup_items[@]}"
chmod 0600 "${backup_path}"
echo "Backup:    ${backup_path}"

repo_git checkout --detach "${target_version}"
chown -R blitzecdn:blitzecdn "${install_dir}"
runuser -u blitzecdn -- "${install_dir}/install.sh"

install -m 0644 packaging/systemd/blitzecdn-api.service /etc/systemd/system/
install -m 0644 packaging/systemd/blitzecdn-cert-renew.service /etc/systemd/system/
install -m 0644 packaging/systemd/blitzecdn-cert-renew.timer /etc/systemd/system/
install -m 0644 packaging/systemd/blitzecdn-drift.service /etc/systemd/system/
install -m 0644 packaging/systemd/blitzecdn-drift.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now "${services[@]}"
/usr/local/bin/blitzecdn doctor

trap - ERR
echo
echo "Updated BlitzeCDN from ${current_version} to ${target_version}."
echo "No edge configuration was deployed. Review with: blitzecdn plan"
