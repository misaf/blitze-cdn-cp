#!/usr/bin/env bash
# Fetch the pinned reference `age` CLI, the independent tool that checks
# BlitzeCDN's encrypted backups open without BlitzeCDN.
#
#   scripts/fetch-age.sh [DIRECTORY]
#
# Puts `age` and `age-keygen` in DIRECTORY (default .state/tools/age) and
# prints that directory. The release archive is checked against the digest
# pinned below before anything is unpacked, so a different file is an error,
# never a different tool. Already there at the pinned version, it does nothing.
#
# To move the pin: set AGE_VERSION, then replace each digest with the one
# GitHub records for the release asset:
#
#   gh api repos/FiloSottile/age/releases/tags/vX.Y.Z \
#     --jq '.assets[] | select(.name|endswith(".tar.gz")) | "\(.digest) \(.name)"'
set -Eeuo pipefail

readonly AGE_VERSION=v1.3.2

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
destination=${1:-${project_dir}/.state/tools/age}

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) platform=linux-amd64
    digest=cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10 ;;
  Linux-aarch64|Linux-arm64) platform=linux-arm64
    digest=6b8dc4333c53a5a57c9e5834e3a48f92605d7154014cd07269ff3327db5d37f4 ;;
  Darwin-x86_64) platform=darwin-amd64
    digest=1d1e4bc66e1427edad7739ae7616157de0e79db8b6d2a1497d7d9925fb06a539 ;;
  Darwin-arm64) platform=darwin-arm64
    digest=e2020b073c44f692685a24d6abc378817eb81ffaaf49fd0531ef8565f767f2f5 ;;
  *) printf 'fetch-age: no pinned age %s for %s-%s\n' "${AGE_VERSION}" \
       "$(uname -s)" "$(uname -m)" >&2
     exit 1 ;;
esac

if [[ -x ${destination}/age && -x ${destination}/age-keygen &&
  $("${destination}/age" --version 2>/dev/null) == "${AGE_VERSION}" ]]; then
  printf '%s\n' "${destination}"
  exit 0
fi

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

work=$(mktemp -d -t blitzecdn-age-XXXXXX)
trap 'rm -rf -- "${work}"' EXIT
asset="age-${AGE_VERSION}-${platform}.tar.gz"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  --output "${work}/${asset}" \
  "https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}/${asset}"
actual=$(sha256 "${work}/${asset}")
if [[ ${actual} != "${digest}" ]]; then
  printf 'fetch-age: %s has digest %s, not the pinned %s\n' \
    "${asset}" "${actual}" "${digest}" >&2
  exit 1
fi
tar -xzf "${work}/${asset}" -C "${work}" age/age age/age-keygen
mkdir -p "${destination}"
for tool in age age-keygen; do
  install -m 0755 "${work}/age/${tool}" "${destination}/${tool}"
done
printf '%s\n' "${destination}"
