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

if [[ ! -d .venv ]]; then
  "${python_command}" -m venv .venv
fi

.venv/bin/pip install .
.venv/bin/pip install 'ansible-core>=2.21,<2.22'
.venv/bin/ansible-galaxy collection install -r ansible/requirements.yml
.venv/bin/blitzecdn setup

echo
echo "Installation complete. Run commands with .venv/bin/blitzecdn"
