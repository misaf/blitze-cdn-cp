#!/usr/bin/env bash
set -euo pipefail

install_dir=/opt/blitzecdn
admin_cidr=
acme_email=
run_deploy=1

usage() {
  cat <<'EOF'
Usage: sudo ./install-standalone.sh --admin-cidr CIDR --email ADDRESS [OPTIONS]

Install one independent BlitzeCDN control plane and edge on this server.

Required:
  --admin-cidr CIDR  Network allowed to administer SSH, for example 203.0.113.8/32
  --email ADDRESS    Default ACME account email

Options:
  --no-deploy        Prepare the node but do not run the initial edge deployment
  -h, --help         Show this help

The checkout must be /opt/blitzecdn because the hardened systemd units use that
path. Re-running the installer preserves the API key, inventory, and state.
EOF
}

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
    --no-deploy)
      run_deploy=0
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

[[ ${EUID} -eq 0 ]] || { echo "error: run this installer with sudo" >&2; exit 1; }
[[ -n ${admin_cidr} ]] || { echo "error: --admin-cidr is required" >&2; exit 2; }
[[ -n ${acme_email} ]] || { echo "error: --email is required" >&2; exit 2; }

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ ${script_dir} == "${install_dir}" ]] || {
  echo "error: clone or copy this release to ${install_dir}, then run the installer there" >&2
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

install -d -m 0750 -o blitzecdn -g blitzecdn "${install_dir}"
chown -R blitzecdn:blitzecdn "${install_dir}"

sudoers_file=/etc/sudoers.d/blitzecdn-deploy
printf '%s\n' 'deploy ALL=(root) NOPASSWD: ALL' > "${sudoers_file}"
chmod 0440 "${sudoers_file}"
visudo -cf "${sudoers_file}" >/dev/null

runuser -u blitzecdn -- "${install_dir}/install.sh"

state_dir="${install_dir}/.state"
key_file="${state_dir}/id_ed25519"
install -d -m 0700 -o blitzecdn -g blitzecdn "${state_dir}"
if [[ ! -f ${key_file} ]]; then
  runuser -u blitzecdn -- ssh-keygen -q -t ed25519 -N '' \
    -C blitzecdn-local-controller -f "${key_file}"
fi

deploy_ssh=/home/deploy/.ssh
authorized_keys="${deploy_ssh}/authorized_keys"
install -d -m 0700 -o deploy -g deploy "${deploy_ssh}"
touch "${authorized_keys}"
chown deploy:deploy "${authorized_keys}"
chmod 0600 "${authorized_keys}"
public_key=$(<"${key_file}.pub")
if ! grep -Fqx -- "${public_key}" "${authorized_keys}"; then
  printf '%s\n' "${public_key}" >> "${authorized_keys}"
fi

controller_ssh=/var/lib/blitzecdn/.ssh
install -d -m 0700 -o blitzecdn -g blitzecdn "${controller_ssh}"
known_hosts="${controller_ssh}/known_hosts"
ssh-keyscan -H localhost 127.0.0.1 2>/dev/null > "${known_hosts}.new"
install -m 0600 -o blitzecdn -g blitzecdn "${known_hosts}.new" "${known_hosts}"
rm -f "${known_hosts}.new"

ssh_config="${controller_ssh}/config"
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

inventory="${install_dir}/ansible/inventory/hosts.yml"
if ! runuser -u blitzecdn -- "${install_dir}/.venv/bin/blitzecdn" edge list --json \
  | grep -Eq '"name"[[:space:]]*:[[:space:]]*"edge-local"'; then
  runuser -u blitzecdn -- "${install_dir}/.venv/bin/blitzecdn" edge add edge-local \
    --host localhost --user deploy --ssh-source "${admin_cidr}"
fi

config_dir=/etc/blitzecdn
environment_file="${config_dir}/blitzecdn.env"
install -d -m 0750 -o root -g blitzecdn "${config_dir}"
generated_api_key=
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
  echo "Keeping existing ${environment_file}; API credentials were not changed."
fi

install -m 0644 packaging/systemd/blitzecdn-api.service /etc/systemd/system/
install -m 0644 packaging/systemd/blitzecdn-cert-renew.service /etc/systemd/system/
install -m 0644 packaging/systemd/blitzecdn-cert-renew.timer /etc/systemd/system/
install -m 0644 packaging/systemd/blitzecdn-drift.service /etc/systemd/system/
install -m 0644 packaging/systemd/blitzecdn-drift.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now blitzecdn-api.service blitzecdn-cert-renew.timer blitzecdn-drift.timer

if [[ ${run_deploy} -eq 1 ]]; then
  runuser -u blitzecdn -- "${install_dir}/.venv/bin/blitzecdn" deploy --yes
else
  echo "Initial deployment skipped. Run: sudo -u blitzecdn ${install_dir}/.venv/bin/blitzecdn deploy"
fi

echo
echo "Standalone BlitzeCDN installation complete."
if [[ -n ${generated_api_key} ]]; then
  echo "API token (save it now): ${generated_api_key}"
fi
echo "Connect: ssh -L 8000:127.0.0.1:8000 deploy@THIS_SERVER"
echo "API:     http://127.0.0.1:8000"
echo "Status:  systemctl status blitzecdn-api.service"
