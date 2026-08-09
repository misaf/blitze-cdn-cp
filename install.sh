#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

python_command="${BLITZECDN_PYTHON:-python3}"
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
collections_path=.state/collections

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
  edge_path="${BLITZECDN_EDGE_PATH}"
  if [[ ! -f "${edge_path}/galaxy.yml" ]]; then
    echo "error: ${edge_path} is not a blitze-cdn-edge checkout (no galaxy.yml)" >&2
    exit 1
  fi

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
