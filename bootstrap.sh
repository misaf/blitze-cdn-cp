#!/usr/bin/env bash
# BlitzeCDN one-command installer.
#
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/misaf/blitze-cdn-cp/HEAD/bootstrap.sh)" \
#     --admin-cidr 203.0.113.8/32 --email ops@example.com
#
# There is no subcommand. Every argument other than --version is passed
# through to `install.sh standalone`, which rejects anything it does not
# recognise — so a stray word here fails the run after the clone.
#
# This script does the little that has to happen before the repository exists on
# the host: check the host can run BlitzeCDN at all, install Git, clone a release
# to /opt/blitzecdn, and hand over to install.sh. Everything after that — the
# packages, the accounts, the SSH trust, the units — belongs to the installer and
# the control-plane role, and nothing here duplicates it.
#
# Three things about install.sh decide the shape of this file:
#
#   * `standalone` refuses to run anywhere but /opt/blitzecdn, so that is where
#     the clone goes;
#   * `update` and `--fresh` refuse to run without /opt/blitzecdn/.git and check
#     the origin URL, so this must be a real clone and not an unpacked tarball;
#   * `standalone` requires --admin-cidr and --email, so this cannot be
#     argument-free the way a package-manager installer is.
#
# Read it before you run it. It runs as root, and by the time install.sh
# finishes this host has a service account, a sudo rule, and public services.
set -Eeuo pipefail

readonly REPOSITORY_URL=https://github.com/misaf/blitze-cdn-cp.git
readonly INSTALL_DIR=/opt/blitzecdn

usage() {
  cat <<'EOF'
Usage: bootstrap.sh --admin-cidr CIDR --email ADDRESS [OPTIONS]

Clone a BlitzeCDN release to /opt/blitzecdn and provision this server as a
standalone control plane and edge.

Required:
  --admin-cidr CIDR  Network allowed to administer SSH, for example 203.0.113.8/32
  --email ADDRESS    Default ACME account email

Options:
  --version vX.Y.Z   Install this release instead of the newest one
  --deploy           Run the initial edge deployment (default: prepare only)
  --public-address ADDRESS
                     Public edge IP or hostname; repeat when needed (NAT safe)
  -h, --help         Show this help

Every option other than --version is passed through to
`install.sh standalone`, which validates them.

To upgrade an existing installation, do not run this script — run
`sudo /opt/blitzecdn/install.sh update`.
EOF
}

die() {
  local status=$1
  shift
  printf '%s\n' "$@" >&2
  exit "${status}"
}

# Fail before the first package where we can. A host that cannot run the control
# plane should learn that from one message, not from a build failure partway
# through provisioning.
require_supported_host() {
  [[ ${EUID} -eq 0 ]] ||
    die 1 "error: run this with sudo — it provisions the whole server"

  [[ -r /etc/os-release ]] ||
    die 1 "error: BlitzeCDN supports Debian and Ubuntu; /etc/os-release is missing"
  # shellcheck disable=SC1091
  source /etc/os-release
  case "${ID:-}" in
    debian | ubuntu) ;;
    *) die 1 "error: unsupported operating system: ${ID:-unknown} (need Debian or Ubuntu)" ;;
  esac

  # The control plane needs Python 3.12 or newer. Debian 12 ships 3.11, so a
  # host that looks supported by distribution version is not necessarily
  # supported at all — and finding that out after the clone wastes the
  # operator's time. When python3 is already present, which is the common case,
  # this costs nothing and writes nothing.
  #
  # There is no upper bound. A ceiling has to be raised for every new Python, and
  # the release that needs it is always discovered on somebody's fresh server
  # rather than in CI. A genuinely incompatible interpreter should fail in the
  # test suite instead.
  if command -v python3 >/dev/null 2>&1; then
    python3 - "${PRETTY_NAME:-${ID}}" <<'PY' || exit 1
import sys

if sys.version_info[:2] < (3, 12):
    running = ".".join(str(part) for part in sys.version_info[:3])
    raise SystemExit(
        f"error: {sys.argv[1]} provides Python {running}, and the BlitzeCDN "
        "control plane needs 3.12 or newer.\n"
        "Nothing was installed. Use a release that ships a supported Python "
        "(Debian 13+ or Ubuntu 24.04+), or install one and re-run with "
        "BLITZECDN_PYTHON pointing at it."
    )
PY
  fi
}

# Resolve the newest vX.Y.Z tag, or confirm the requested one exists. Release
# tags rather than the default branch: a branch moves, and an operator who runs
# this twice a week apart should be able to say which release each host got.
resolve_version() {
  local requested=$1

  # Keep git's own diagnosis. Discarding it turns "repository not found",
  # "could not resolve host", and an expired credential into one misleading
  # message about missing release tags.
  local refs
  refs=$(git ls-remote --tags --refs "${REPOSITORY_URL}" 'v*' 2>&1) ||
    die 1 "error: cannot read release tags from ${REPOSITORY_URL}" "${refs}"

  local -a tags
  mapfile -t tags < <(
    printf '%s\n' "${refs}" |
      sed -n 's#.*refs/tags/\(v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$#\1#p' |
      sort -V
  )
  [[ ${#tags[@]} -gt 0 ]] ||
    die 1 "error: ${REPOSITORY_URL} has no stable vX.Y.Z release tags"

  if [[ -z ${requested} ]]; then
    printf '%s\n' "${tags[-1]}"
    return 0
  fi
  # Membership in a space-delimited list, matched as a glob: a quoted =~
  # right-hand side already matches literally, which reads as a regex but is not.
  [[ " ${tags[*]} " == *" ${requested} "* ]] ||
    die 1 "error: ${requested} is not a release tag on origin"
  printf '%s\n' "${requested}"
}

main() {
  local requested_version=''
  local -a passthrough=()
  local have_admin_cidr=0 have_email=0

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --version)
        [[ $# -ge 2 ]] || die 2 "error: --version needs a value"
        # Format now, existence later: a typo is invalid input and should not
        # need root, or a network round trip, to be reported.
        [[ $2 =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
          die 2 "error: --version must use the vX.Y.Z format"
        requested_version=$2
        shift 2
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        [[ $1 == --admin-cidr ]] && have_admin_cidr=1
        [[ $1 == --email ]] && have_email=1
        passthrough+=("$1")
        shift
        ;;
    esac
  done

  # Presence only — install.sh owns the format rules. Checking here means a
  # missing flag costs nothing, instead of leaving a clone behind that the next
  # run would refuse to overwrite.
  [[ ${have_admin_cidr} -eq 1 ]] || { usage >&2; die 2 "error: --admin-cidr is required"; }
  [[ ${have_email} -eq 1 ]] || { usage >&2; die 2 "error: --email is required"; }

  require_supported_host

  if [[ -e ${INSTALL_DIR} ]]; then
    die 1 \
      "error: ${INSTALL_DIR} already exists; this script only makes new installations" \
      "To upgrade:      sudo ${INSTALL_DIR}/install.sh update" \
      "To re-provision: sudo ${INSTALL_DIR}/install.sh standalone --admin-cidr CIDR --email ADDRESS" \
      "To start over:   sudo ${INSTALL_DIR}/install.sh --fresh"
  fi

  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y git

  local version
  version=$(resolve_version "${requested_version}")

  echo
  echo "Repository: ${REPOSITORY_URL}"
  echo "Release:    ${version}"
  echo "Target:     ${INSTALL_DIR}"
  echo

  git clone --depth 1 --branch "${version}" "${REPOSITORY_URL}" "${INSTALL_DIR}"

  local tagged_version
  tagged_version=$(python3 - "${INSTALL_DIR}/pyproject.toml" <<'PY'
import pathlib
import sys
import tomllib

document = tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print("v" + document["project"]["version"])
PY
  )
  # The same check `install.sh update` makes: a tag whose package version
  # disagrees with it is a mis-cut release, and this is the last cheap moment to
  # notice. Remove the clone so a retry is not blocked by a directory that
  # exists.
  if [[ ${tagged_version} != "${version}" ]]; then
    rm -rf -- "${INSTALL_DIR}"
    die 1 "error: ${version} contains package version ${tagged_version}"
  fi

  echo "Commit:     $(git -C "${INSTALL_DIR}" rev-parse --short HEAD)"
  echo

  # exec so the installer's exit code is this script's exit code, and so the
  # documented CLI codes reach whatever ran the one-liner.
  exec "${INSTALL_DIR}/install.sh" standalone "${passthrough[@]}"
}

main "$@"
