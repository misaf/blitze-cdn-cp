#!/usr/bin/env bash
# Report each collection pinned by requirements.yml and whether `.state/collections`
# holds it at exactly that version.
#
#   scripts/collections-check.sh
#
# Prints one line per pin, `NAME VERSION ok` or `NAME VERSION missing`, and
# exits 1 if any pin is missing. `just ansible-check` runs it first so a missing
# collection fails with "run `just collections`" rather than as an unresolved
# module, and `scripts/doctor.sh` reads its lines so both ask the same question
# the same way.
set -uo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "${project_dir}" || exit 2

readonly COLLECTIONS_DIR=.state/collections/ansible_collections
readonly REQUIREMENTS=src/blitzecdn/assets/ansible/requirements.yml

status=0
while read -r name version; do
  manifest="${COLLECTIONS_DIR}/${name/.//}/MANIFEST.json"
  if [[ -f ${manifest} ]] && grep -q "\"version\": \"${version}\"" "${manifest}"; then
    printf '%s %s ok\n' "${name}" "${version}"
  else
    printf '%s %s missing\n' "${name}" "${version}"
    status=1
  fi
done < <(awk '
  /- name:/ { name = $3 }
  /version:/ && name { gsub(/"/, "", $2); print name, $2; name = "" }
' "${REQUIREMENTS}")
exit "${status}"
