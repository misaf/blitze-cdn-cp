#!/usr/bin/env bash
# Run the whole installer lifecycle against a throwaway container.
#
#   tests/container-install.sh ubuntu:26.04
#
# The only test that runs `install.sh standalone` for real: systemd as PID 1,
# real apt, real accounts. A standalone host is also an edge, so it must meet
# the narrower edge platform; CI runs Ubuntu only.
#
# Requires Docker and about five minutes per image.
#
# Both images are built here from the working tree unless an archive of each
# is given, for a machine that should not build them itself, such as one that
# cannot reach PyPI:
#
#   CONTROL_PLANE_IMAGE_ARCHIVE  `docker save` of the pinned control-plane tag
#   EDGE_IMAGE_ARCHIVE           `docker save` of blitzecdn-edge:standalone,
#                                built with `blitzecdn edge image spec`
set -Eeuo pipefail

readonly IMAGE=${1:?usage: container-install.sh IMAGE}
readonly ADMIN_CIDR=203.0.113.8/32
readonly ACME_EMAIL=ops@example.com
# The API access list, and the one a later run repoints it to. Neither contains
# this container's address, so the gate must refuse the harness.
readonly ALLOWED_IPS=203.0.113.8/32,198.51.100.0/24
readonly REPOINTED_IPS=198.51.100.0/24
# A CA that speaks ACME, for the issuance stage. Pinned, not :latest.
readonly PEBBLE_IMAGE=ghcr.io/letsencrypt/pebble:2.10.1
readonly CHALLTESTSRV_IMAGE=ghcr.io/letsencrypt/pebble-challtestsrv:2.10.1
# The ACME hostname: the record label joined to the zone, so issuance asks for
# exactly the name the deploy serves. The derived virtual host is the zone with
# dots turned into hyphens.
#
# example.com rather than example.test: the deploy refuses an ACME certificate
# for a reserved suffix (.test, .example, ...), since no public CA issues for
# one. example.com is reserved for documentation but not refused, and every
# lookup here goes to the challenge DNS server, never to the real zone.
readonly ACME_SITE=example-com
readonly ACME_DOMAIN=cdn.example.com

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
container="blitzecdn-$(printf '%s' "${IMAGE}" | tr -c 'a-z0-9' '-')-$$"
archive=$(mktemp -t blitzecdn-source-XXXXXX).tgz
image_archive=$(mktemp -t blitzecdn-control-plane-XXXXXX).tar
pebble_config=$(mktemp -t blitzecdn-pebble-XXXXXX).json

cleanup() {
  # KEEP_CONTAINER=1 leaves the host up to inspect a failure; remove it with
  # `docker rm -f -v` afterwards.
  if [[ ${KEEP_CONTAINER:-0} == 1 ]]; then
    printf 'Kept %s\n' "${container}" >&2
  else
    # `-v`: the host's anonymous volumes go with it. They hold a whole
    # container engine's images, and a runner that keeps them keeps gigabytes.
    docker rm -f -v "${container}" >/dev/null 2>&1 || true
  fi
  rm -f -- "${archive}" "${image_archive}" "${pebble_config}"
}
trap cleanup EXIT

say() { printf '\n=== %s ===\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

in_container() { docker exec "${container}" bash -c "$1"; }
# Same, but with stdin attached, for streaming an image into the host's engine.
into_container() { docker exec -i "${container}" bash -c "$1"; }

# Whatever the edge presents for the ACME hostname, through the requested `x509`
# fields. Certificate assertions read the wire, not the store.
served_certificate() {
  in_container "openssl s_client -connect 127.0.0.1:443 -servername ${ACME_DOMAIN} \
    </dev/null 2>/dev/null | openssl x509 -noout $*"
}

say "Starting ${IMAGE} with systemd"
# Anonymous volumes for /var/lib/docker and /var/lib/containerd: the nested
# engine's overlay mounts cannot sit on the outer container's overlay rootfs
# (they fail with a bare `invalid argument`), as with `docker:dind`.
docker run -d --name "${container}" \
  --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --tmpfs /run --tmpfs /run/lock \
  -v /var/lib/docker -v /var/lib/containerd \
  "${IMAGE}" \
  bash -c 'apt-get update -qq && apt-get install -y -qq systemd systemd-sysv >/dev/null && exec /sbin/init' \
  >/dev/null

# Poll for systemd. "degraded" is accepted: unrelated units routinely fail in a
# container.
for _ in $(seq 60); do
  state=$(in_container 'systemctl is-system-running' 2>/dev/null || true)
  [[ ${state} == running || ${state} == degraded ]] && break
  sleep 2
done
[[ ${state} == running || ${state} == degraded ]] ||
  fail "systemd never came up in ${IMAGE} (last state: ${state:-unknown})"

say "Copying the working tree to /opt/blitzecdn"
# The working tree, not a clone, so CI tests the commit under review, without
# runtime state, caches, local configuration or host metadata. macOS tar would
# otherwise add AppleDouble `._*.py` files, which Alembic reads as revisions.
COPYFILE_DISABLE=1 tar \
  --exclude=.venv --exclude='.venv.invalid.*' \
  --exclude=.state --exclude=.git --exclude=.ansible --exclude=.cache \
  --exclude=.env --exclude=blitzecdn.toml --exclude=.codex \
  --exclude=dist --exclude='*.egg-info' \
  --exclude=.mypy_cache --exclude=.pytest_cache --exclude=.ruff_cache \
  --exclude=.hypothesis --exclude=.coverage --exclude=coverage.xml \
  --exclude=htmlcov --exclude=.DS_Store --exclude='._*' \
  --exclude='*.pyc' --exclude=__pycache__ \
  -czf "${archive}" -C "${project_dir}" . 2>/dev/null
in_container 'mkdir -p /opt/blitzecdn'
# Not /tmp: newer images mount a tmpfs over it, hiding what docker cp put there.
docker cp "${archive}" "${container}:/root/source.tgz" >/dev/null
in_container 'tar -xzf /root/source.tgz -C /opt/blitzecdn' 2>/dev/null

say "Building the control-plane image this host will install"
# From the working tree, under the reference the release pins, and handed to
# the installer as an archive: the commit under review is what gets installed,
# and the installer's own pull is the one step this harness does not exercise.
pinned_image=$(sed -n 's/^blitzecdn_controlplane_image: *//p' \
  "${project_dir}/src/blitzecdn/assets/ansible/roles/blitzecdn_controlplane/defaults/main.yml")
[[ -n ${pinned_image} ]] || fail "no control-plane image is pinned"
if [[ -n ${CONTROL_PLANE_IMAGE_ARCHIVE:-} ]]; then
  docker load --input "${CONTROL_PLANE_IMAGE_ARCHIVE}" >/dev/null ||
    fail "could not load ${CONTROL_PLANE_IMAGE_ARCHIVE}"
  docker image inspect "${pinned_image}" >/dev/null 2>&1 ||
    fail "${CONTROL_PLANE_IMAGE_ARCHIVE} does not hold ${pinned_image}"
else
  docker build --quiet \
    --file "${project_dir}/src/blitzecdn/assets/docker/control-plane/Dockerfile" \
    --tag "${pinned_image}" "${project_dir}" >/dev/null ||
    fail "could not build the control-plane image"
fi
docker save --output "${image_archive}" "${pinned_image}" ||
  fail "could not export the control-plane image"
docker cp "${image_archive}" "${container}:/root/control-plane.tar" >/dev/null

# An installation must not need PyPI: the network this exists for drops it.
# Both names resolve nowhere, so any uv or pip that ran would fail the install.
in_container 'printf "0.0.0.0 pypi.org files.pythonhosted.org\n:: pypi.org files.pythonhosted.org\n" >> /etc/hosts' ||
  fail "could not cut this host off from PyPI"

say "Installing"
if ! in_container "cd /opt/blitzecdn && BLITZECDN_CONTROL_PLANE_ARCHIVE=/root/control-plane.tar ./install.sh standalone --admin-cidr ${ADMIN_CIDR} --email ${ACME_EMAIL} --allowed-ips ${ALLOWED_IPS}"; then
  # Preserve the evidence before the EXIT trap removes the disposable host.
  # This catches corrupt source/package copies that otherwise surface only as
  # an opaque import error several layers inside the installer.
  # The expansions belong to the container shell.
  # shellcheck disable=SC2016
  in_container 'for path in /opt/blitzecdn/src/blitzecdn/assets/migrations/versions/0001_initial_core.py /opt/blitzecdn/.venv/lib/python3.*/site-packages/blitzecdn/assets/migrations/versions/0001_initial_core.py; do printf "%s bytes=" "$path"; wc -c < "$path"; done' || true
  in_container 'sha256sum /opt/blitzecdn/src/blitzecdn/assets/migrations/versions/0001_initial_core.py /opt/blitzecdn/.venv/lib/python3.*/site-packages/blitzecdn/assets/migrations/versions/0001_initial_core.py' || true
  fail "install failed on ${IMAGE}"
fi

say "Checking what the installation produced"
# Everything came from the image: no virtualenv, no uv, no Galaxy collections.
in_container 'test ! -e /opt/blitzecdn/.venv && test ! -e /opt/blitzecdn/.state' ||
  fail "the installer built host tooling in the checkout"
in_container '! command -v uv >/dev/null' || fail "the installer put uv on this host"
in_container "docker inspect -f '{{.Config.Image}}' blitzecdn-api | grep -qF '${pinned_image}'" ||
  fail "the API does not run the image the installer was given"
in_container '! getent passwd blitzecdn >/dev/null' || fail "unexpected blitzecdn host account"
in_container '! getent group blitzecdn >/dev/null' || fail "unexpected blitzecdn host group"
in_container 'getent passwd deploy >/dev/null' || fail "no deploy account"
in_container 'test -x /usr/local/bin/blitzecdn' || fail "no CLI wrapper"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-api | grep -qx healthy' || {
  in_container 'docker compose --file /etc/blitzecdn/control-plane.compose.yml ps' || true
  in_container 'docker compose --file /etc/blitzecdn/control-plane.compose.yml logs blitzecdn-api' || true
  fail "API not running"
}
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-worker | grep -qx healthy' || fail "worker not running"
for service in blitzecdn-api blitzecdn-worker; do
  in_container "docker inspect -f '{{.Config.User}}' ${service} | grep -qx nobody:nogroup" ||
    fail "${service} does not declare the reused image identity"
  in_container "docker exec ${service} id | grep -q 'uid=65534(nobody) gid=65534(nogroup)'" ||
    fail "${service} is not running as nobody:nogroup"
done
in_container 'docker compose --file /etc/blitzecdn/control-plane.compose.yml run --rm --no-deps --entrypoint id blitzecdn-cli | grep -q "uid=65534(nobody) gid=65534(nogroup)"' ||
  fail "CLI is not running as nobody:nogroup"
in_container 'stat -c %u:%g:%a /var/lib/blitzecdn | grep -qx 65534:65534:700' ||
  fail "persistent state ownership does not match the container identity"
in_container 'stat -c %u:%g:%a /var/backups/blitzecdn | grep -qx 65534:65534:700' ||
  fail "backup ownership does not match the container identity"
in_container 'stat -c %U:%G:%a /etc/blitzecdn/blitzecdn.env | grep -qx root:root:600' ||
  fail "service secrets are not restricted to host root"
in_container 'stat -c %U:%G:%a /etc/blitzecdn/blitzecdn.toml | grep -qx root:root:644' ||
  fail "non-secret configuration is not host-managed"
in_container 'docker exec blitzecdn-api python -c "import sqlite3; p=\"/opt/blitzecdn/.state/permission-test.db\"; c=sqlite3.connect(p); assert c.execute(\"PRAGMA journal_mode=WAL\").fetchone()[0] == \"wal\"; c.execute(\"CREATE TABLE writable (id INTEGER)\"); c.commit(); c.close()"' ||
  fail "SQLite could not create and write WAL state as the application identity"
in_container 'test -f /var/lib/blitzecdn/permission-test.db && rm -f /var/lib/blitzecdn/permission-test.db*' ||
  fail "SQLite permission test did not write into persistent state"
# From / rather than the checkout: the wrapper exists to make that work.
in_container 'cd / && blitzecdn --version >/dev/null' || fail "CLI unusable outside the checkout"
in_container 'cd / && blitzecdn doctor --json >/dev/null' || fail "doctor failed"

say "Checking the API access list the installer was given"
# The one place the access list is exercised end to end: the flag reached the
# role, the role wrote the file, the API read it, and the gate decides.
in_container "grep -qx 'BLITZE_ALLOWED_IPS=${ALLOWED_IPS}' /etc/blitzecdn/blitzecdn.env" ||
  fail "the installer did not seed the API access list"
in_container "cd / && blitzecdn doctor --json | grep -q '203.0.113.8/32'" ||
  fail "doctor does not report the API access list"
# A non-empty list binds publicly, so a request from this host's own address
# connects and is refused by the gate. The code is compared because a listener
# still on loopback would fail to connect instead.
# shellcheck disable=SC2016
in_container 'test "$(curl -s -o /dev/null -w "%{http_code}" "http://$(hostname):8000/health")" = 403' ||
  fail "the API did not refuse an unlisted client on its public address"
# The gate's own exemption, which keeps the health check and SSH tunnels
# working.
in_container 'curl -sf http://127.0.0.1:8000/health >/dev/null' ||
  fail "the API stopped answering on loopback"

say "Seeding the edge runtime image this host will run"
# The edge runs from an image built from the commit under review and loaded into
# the disposable host's engine. That needs the engine first, so its repository
# is seeded exactly as blitzecdn_docker seeds it (same deb822 file), and the
# converge still reports no change on its second run.
#
# The module set is the installed release's own `blitzecdn edge image spec`,
# as the publishing workflow builds it. Without it the image carries no
# capability module and the deploy's module check refuses it.
if [[ -n ${EDGE_IMAGE_ARCHIVE:-} ]]; then
  docker load --input "${EDGE_IMAGE_ARCHIVE}" >/dev/null ||
    fail "could not load ${EDGE_IMAGE_ARCHIVE}"
  docker image inspect blitzecdn-edge:standalone >/dev/null 2>&1 ||
    fail "${EDGE_IMAGE_ARCHIVE} does not hold blitzecdn-edge:standalone"
else
  module_args=()
  while IFS= read -r argument; do
    if [[ -n ${argument} ]]; then
      module_args+=(--build-arg "${argument}")
    fi
  done < <(in_container 'cd / && blitzecdn edge image spec')
  [[ ${#module_args[@]} -gt 0 ]] ||
    fail "blitzecdn edge image spec named no build arguments"
  docker build --quiet "${module_args[@]}" --tag blitzecdn-edge:standalone \
    "${project_dir}/src/blitzecdn/assets/docker/edge" >/dev/null ||
    fail "could not build the edge runtime image"
fi

in_container 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl gnupg >/dev/null' ||
  fail "could not install the Docker repository prerequisites"
# Both URLs carry the running distribution, as the play composes them from
# `ansible_distribution | lower`.
# shellcheck disable=SC2016
in_container 'install -d -m 0755 /etc/apt/keyrings && curl -fsSL --proto "=https" --tlsv1.2 "https://download.docker.com/linux/$(. /etc/os-release && printf %s "${ID}")/gpg" -o /etc/apt/keyrings/docker.asc && chmod 0644 /etc/apt/keyrings/docker.asc' ||
  fail "could not fetch the Docker signing key"
# shellcheck disable=SC2016
in_container 'printf "Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n" "$(. /etc/os-release && printf %s "${ID}")" "$(. /etc/os-release && printf %s "${VERSION_CODENAME}")" "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/docker.sources' ||
  fail "could not configure the Docker repository"
in_container 'DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null' ||
  fail "could not install Docker Engine"
in_container 'systemctl enable --now docker' || fail "Docker did not start"
docker save blitzecdn-edge:standalone | into_container 'docker load' >/dev/null ||
  fail "could not load the edge runtime image into the disposable host"

# The raw Ansible output for the last run, where a check-mode diff lands. Read
# on the host at `/var/lib/blitzecdn`, the bind mount behind the CLI
# container's `/opt/blitzecdn/.state`.
dump_ansible_log() {
  # shellcheck disable=SC2016
  in_container 'set -- /var/lib/blitzecdn/logs/*.log
    if [ ! -e "$1" ]; then
      printf "No Ansible logs under /var/lib/blitzecdn/logs\n"
      ls -la /var/lib/blitzecdn || true
      exit 0
    fi
    latest=$(ls -t /var/lib/blitzecdn/logs/*.log | head -1)
    printf "Ansible log: %s\n\n" "$latest"
    # The changed tasks and nothing else. The play is several hundred `ok:`
    # lines that say nothing about a disagreement, and the head of it is the
    # argument-spec validation for roles that agreed — which is all the first
    # version of this printed. Ansible writes each diff immediately above the
    # `changed:` line it belongs to, so the context is the report.
    grep -B 40 -A 1 "^changed: \[" "$latest" || {
      # The other way this ends. A check-mode run reports a diff or it reports
      # a failure, and printing only the first left the second as "not a task
      # diff" and a recap of counts -- a report with the cause missing, which
      # is how a supported-platform assert reads as an unexplained failure.
      printf "No changed tasks in the log; the failure is not a task diff.\n\n"
      grep -B 2 -A 20 "^fatal: \|^failed: " "$latest" ||
        printf "No failed tasks either; the run ended without a diff or a failure.\n"
    }
    printf "\n--- recap ---\n"
    sed -n "/^PLAY RECAP/,\$p" "$latest"' || true
}

say "Converging this host as an edge"
# The one place an edge role is executed rather than checked by shape. The
# post-bootstrap CLI handoff records edge-local; this verifies it instead of
# adding a second row for the same machine.
in_container 'cd / && blitzecdn edge list --json | grep -q '"'"'"name": "edge-local"'"'"'' ||
  fail "the installer did not register the local edge"
# Apply, or show why not: the deployment record and every failed task. The
# JSON is the evidence, so it is kept rather than sent to /dev/null.
deploy_or_fail() {
  local environment=$1 message=$2 output
  # --wait: the worker's own background runs (startup recovery, the drift
  # check, certificate reconciliation) take the deployment lock too, and a
  # test that has just restarted the services must not race them for it.
  if ! output=$(in_container "cd / && ${environment} blitzecdn deploy --yes --wait 300 --json"); then
    printf '%s\n' "${output}" | tail -80
    # shellcheck disable=SC2016
    in_container 'latest=$(ls -t /var/lib/blitzecdn/logs/*.log 2>/dev/null | head -1)
      [ -n "$latest" ] && grep -B 2 -A 20 "^fatal: \|^failed: " "$latest"' || true
    fail "${message}"
  fi
}

# Fleet policy, set the way an operator sets it: the edge image is a database
# setting the inventory plugin publishes.
in_container 'cd / && blitzecdn config set blitzecdn_edge_image blitzecdn-edge:standalone' ||
  fail "could not pin the edge runtime image"
in_container 'cd / && blitzecdn config set blitzecdn_edge_stack_image_pull false' ||
  fail "could not disable the registry pull"

# The zone carries the policy; the proxied record carries the origin. The
# virtual host is named after the zone (`example-com`), hence `${ACME_SITE}`.
in_container 'cd / && blitzecdn domain add example.com' ||
  fail "could not add a zone"
in_container 'cd / && blitzecdn record add example.com cdn --value 127.0.0.1' ||
  fail "could not put the hostname on the edge"

# Check mode first: it must survive a host that has never converged.
in_container 'cd / && blitzecdn plan --wait 300 --json >/dev/null' || {
  # shellcheck disable=SC2016
  dump_ansible_log
  fail "check-mode run failed"
}

deploy_or_fail "" "deploy failed"
in_container "test -f /etc/blitzecdn/releases/current/nginx/sites-enabled/${ACME_SITE}.conf" ||
  fail "the deploy did not enable the managed site"
in_container 'docker exec blitzecdn-edge nginx -t' ||
  fail "the converged nginx configuration does not load"
in_container "grep -q '^${ACME_SITE}\$' /etc/blitzecdn/releases/current/nginx/blitzecdn-managed-sites" ||
  fail "the managed-site registry was not written"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-edge | grep -qx healthy' ||
  fail "the edge container is not healthy"
# No traffic-serving BlitzeCDN packages on the host: a host process would
# compete with the container for public ports.
in_container '! command -v nginx' || fail "nginx was installed on the host"

# Converging twice must change nothing: the drift check is the assertion.
deploy_or_fail "" "second deploy failed"
in_container 'cd / && blitzecdn drift --wait 300 --json' || {
  # shellcheck disable=SC2016
  dump_ansible_log
  # The two files the stack renders itself from. A check-mode diff says which
  # line disagrees; these say what the converge actually settled on, which is
  # the other half of the comparison and is not in any log.
  in_container 'printf "\n--- compose file on the host ---\n"
    cat /etc/blitzecdn/compose.yml 2>&1 | grep -vE "^ *#|^$"
    printf "\n--- recorded runtime image ---\n"
    cat /var/lib/blitzecdn/edge/image 2>&1' || true
  fail "the fleet reports drift immediately after converging"
}

say "Issuing a certificate from a real ACME server"
# The control plane's half of ACME: certbot, the manual hooks, the playbook they
# drive, and the chain the store validates and installs, against Pebble, a
# throwaway CA that validates HTTP-01 for real. Serving a challenge alone is the
# HTTP/3 harness's job. Preflight is skipped by the request (this edge is not
# public) and runs during renewal below.

# Asserted, so a hostname mismatch fails here rather than as an ACME timeout.
in_container "grep -q 'server_name ${ACME_DOMAIN};' /etc/blitzecdn/releases/current/nginx/sites-enabled/${ACME_SITE}.conf" ||
  fail "the deployed site does not serve ${ACME_DOMAIN}"

# Pulled here and streamed in, like the edge image: the host needs no registry
# credentials.
docker pull --quiet "${PEBBLE_IMAGE}" >/dev/null || fail "could not pull ${PEBBLE_IMAGE}"
docker pull --quiet "${CHALLTESTSRV_IMAGE}" >/dev/null ||
  fail "could not pull ${CHALLTESTSRV_IMAGE}"
docker save "${PEBBLE_IMAGE}" "${CHALLTESTSRV_IMAGE}" | into_container 'docker load' >/dev/null ||
  fail "could not load the ACME server images into the disposable host"

# Pebble's API certificate: the image ships only a binary, so config and
# certificates are supplied here.
in_container 'mkdir -p /root/pebble && openssl req -x509 -newkey rsa:2048 -sha256 -nodes \
  -days 1 -keyout /root/pebble/key.pem -out /root/pebble/cert.pem -subj /CN=localhost \
  -addext subjectAltName=IP:127.0.0.1,DNS:localhost 2>/dev/null' ||
  fail "could not generate the ACME server certificate"
# `httpPort` 80: Pebble validates against the edge's real listener.
cat > "${pebble_config}" <<'JSON'
{
  "pebble": {
    "listenAddress": "127.0.0.1:14000",
    "managementListenAddress": "127.0.0.1:15000",
    "certificate": "/pebble/cert.pem",
    "privateKey": "/pebble/key.pem",
    "httpPort": 80,
    "tlsPort": 443,
    "ocspResponderURL": "",
    "externalAccountBindingRequired": false
  }
}
JSON
docker cp "${pebble_config}" "${container}:/root/pebble/config.json" >/dev/null ||
  fail "could not install the ACME server configuration"

# Every name resolves here, so Pebble validates where the edge serves; its own
# challenge responders are off. On 127.0.0.1:53 because `preflight_dns_servers`
# takes addresses on one port, and systemd-resolved holds 127.0.0.53:53.
in_container "docker run -d --name pebble-dns --network host ${CHALLTESTSRV_IMAGE} \
  -dnsserver 127.0.0.1:53 -http01 '' -https01 '' -tlsalpn01 ''" >/dev/null ||
  fail "could not start the challenge DNS server"
# `docker run -d` succeeds for a container that dies at once, so ask the server
# the question the CA and preflight will ask.
in_container 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dnsutils >/dev/null' ||
  fail "could not install a DNS client"
resolved=$(in_container "dig +short +time=2 +tries=2 @127.0.0.1 ${ACME_DOMAIN}") ||
  fail "the challenge DNS server did not answer"
[[ ${resolved} == "127.0.0.1" ]] || {
  in_container 'docker logs pebble-dns' || true
  in_container 'docker inspect -f "{{.State.Running}} {{.State.ExitCode}}" pebble-dns' || true
  fail "the challenge DNS server answered '${resolved}' for ${ACME_DOMAIN}"
}
# NOSLEEP and NONCEREJECT disable Pebble's random delays and nonce rejections.
# AUTHZREUSE, so renewal always revalidates over HTTP-01.
in_container "docker run -d --name pebble --network host -v /root/pebble:/pebble:ro \
  -e PEBBLE_VA_NOSLEEP=1 -e PEBBLE_WFE_NONCEREJECT=0 -e PEBBLE_AUTHZREUSE=0 \
  ${PEBBLE_IMAGE} -config /pebble/config.json -dnsserver 127.0.0.1:53" >/dev/null ||
  fail "could not start the ACME server"

for _ in $(seq 30); do
  in_container 'curl -sSk --max-time 2 https://127.0.0.1:14000/dir >/dev/null 2>&1' && break
  sleep 1
done
in_container 'curl -sSk --max-time 5 https://127.0.0.1:14000/dir >/dev/null' || {
  in_container 'docker logs pebble' || true
  fail "the ACME server never answered"
}

# `certbot` is this capability's setting naming an executable, so a wrapper
# reaches a different CA. The wrapper is in the image, built from the checkout.
in_container 'printf "certbot = \"/opt/blitzecdn/tests/integration/certbot-pebble\"\n" \
  >> /etc/blitzecdn/blitzecdn.toml' ||
  fail "could not point the control plane at the test certbot"
# Preflight asks public DNS, which knows nothing of this edge, and renewal
# has no override, so preflight is pointed at the CA's DNS server. Replaced
# rather than appended: a duplicate TOML key is a parse error.
in_container 'sed -i "s|^preflight_dns_servers = .*|preflight_dns_servers = [\"127.0.0.1\"]|" \
  /etc/blitzecdn/blitzecdn.toml' ||
  fail "could not point preflight at the challenge DNS server"
in_container 'grep -q "^preflight_dns_servers = \[\"127.0.0.1\"\]$" /etc/blitzecdn/blitzecdn.toml' ||
  fail "the preflight resolver was not rewritten"
# Configuration is read at start, and nothing in the Compose file changed, so
# `--force-recreate` is needed; plain `up` would leave the old processes on the
# old configuration (the real Let's Encrypt).
started_before=$(in_container 'docker inspect -f "{{.State.StartedAt}}" blitzecdn-api')
in_container 'docker compose --file /etc/blitzecdn/control-plane.compose.yml \
  up --detach --force-recreate --wait --wait-timeout 180 blitzecdn-api blitzecdn-worker' >/dev/null || {
  in_container 'docker compose --file /etc/blitzecdn/control-plane.compose.yml logs blitzecdn-api' || true
  fail "the control plane did not come back with the test certbot configured"
}
# Check the restart took effect, or everything below runs against the wrong CA.
started_after=$(in_container 'docker inspect -f "{{.State.StartedAt}}" blitzecdn-api')
[[ ${started_before} != "${started_after}" ]] ||
  fail "the API was not replaced, so it is still running the old configuration"

# Issuance is an API operation. The key is read inside the container and never
# crosses into this shell.
# shellcheck disable=SC2016
issued=$(in_container 'key=$(sed -n "s/^BLITZE_API_KEYS=operator://p" /etc/blitzecdn/blitzecdn.env)
  [ -n "${key}" ] || { printf "no operator API key in blitzecdn.env\n" >&2; exit 1; }
  curl -sS --max-time 600 -X POST -H "X-API-Key: ${key}" -H "Content-Type: application/json" \
    -d "{\"skip_preflight\": true}" \
    http://127.0.0.1:8000/v3/hosts/'"${ACME_SITE}"'/certificate/request') || {
  # certbot's own log first: it names the ACME server it contacted and the
  # problem document it got back, which is the difference between "the CA
  # refused" and "the CA was never the one this stage started".
  in_container 'tail -40 /var/lib/blitzecdn/letsencrypt/logs/letsencrypt.log' || true
  dump_ansible_log
  in_container 'docker logs --tail 40 pebble' || true
  fail "the certificate request did not complete"
}
printf '%s' "${issued}" | grep -Eq '"source": ?"acme"' || {
  printf 'response: %s\n' "${issued}"
  in_container 'tail -40 /var/lib/blitzecdn/letsencrypt/logs/letsencrypt.log' || true
  in_container 'docker logs --tail 40 pebble' || true
  dump_ansible_log
  fail "the control plane did not record an ACME certificate"
}
printf '%s' "${issued}" | grep -q "${ACME_DOMAIN}" ||
  fail "the issued certificate does not cover ${ACME_DOMAIN}"

# `ssl_mode` defaults to off and needs an active certificate, so it is set now,
# in the operator's real order. Flexible because the origin speaks plain HTTP.
in_container "cd / && blitzecdn domain ssl example.com --mode flexible --json >/dev/null" ||
  fail "could not turn on TLS for the site that now has a certificate"

# A deploy puts the certificate on the edge; until then the site is HTTP only.
deploy_or_fail "" "the deploy that installs the certificate failed"

in_container 'curl -sSk --max-time 5 https://127.0.0.1:15000/roots/0 -o /root/pebble/root.pem' ||
  fail "could not fetch the ACME root"
# The chain, checked as a client would: it verifies to Pebble's root and is
# valid for this name. A handshake rather than a request, since the origin is
# this host and a request would loop.
in_container "openssl s_client -connect 127.0.0.1:443 -servername ${ACME_DOMAIN} \
  -CAfile /root/pebble/root.pem -verify_return_error -verify_hostname ${ACME_DOMAIN} \
  </dev/null >/dev/null 2>&1" || {
  in_container "openssl s_client -connect 127.0.0.1:443 -servername ${ACME_DOMAIN} \
    </dev/null 2>/dev/null | openssl x509 -noout -issuer -subject -dates" || true
  # Whether anything is listening at all, and with which files. An empty
  # certificate above is either no HTTPS server block for this name or no
  # handshake, and these two say which.
  in_container "grep -E 'listen|ssl_certificate' /etc/blitzecdn/releases/current/nginx/sites-enabled/${ACME_SITE}.conf" || true
  in_container 'cd / && blitzecdn cert list --json' || true
  in_container 'docker logs --tail 40 pebble' || true
  fail "the edge is not serving a certificate that validates against the ACME root"
}
# Print what was served. The SAN, not the subject: Pebble issues without a
# common name. `-verify_hostname` above is what checks the name.
served_certificate -issuer -ext subjectAltName -enddate ||
  fail "could not read back the certificate that just verified"


say "Renewing that certificate the way the timer does"
# Renewal picks its own candidates, goes through preflight with no override, and
# must leave the edge serving a different certificate. Preflight is asserted
# first: failing there would show as a skipped site and exit zero.
in_container "cd / && blitzecdn cert preflight ${ACME_SITE}" ||
  fail "preflight blocks issuance for a site the edge is already serving"

renewed_from=$(served_certificate -fingerprint -sha256) ||
  fail "could not read the certificate the edge is serving"

# --force because nothing is due yet; --deploy because a renewal must reach the
# edge.
in_container "cd / && blitzecdn cert renew --force --site ${ACME_SITE} --deploy --json >/dev/null" || {
  in_container 'tail -40 /var/lib/blitzecdn/letsencrypt/logs/letsencrypt.log' || true
  in_container 'docker logs --tail 40 pebble' || true
  fail "the renewal did not complete"
}

renewed_to=$(served_certificate -fingerprint -sha256) ||
  fail "could not read the certificate the edge serves after renewing"
# The assertion that separates a renewal from a no-op that reports success.
[[ ${renewed_from} != "${renewed_to}" ]] || {
  printf 'still serving: %s\n' "${renewed_to}"
  # Which of the three hops kept the old bytes. The store's own view, the file
  # the deploy is supposed to have replaced, what Nginx has loaded, and the
  # deploy's changed tasks — a renewal that reached the store but not the disk
  # and one that reached the disk but not a reload look identical from the
  # wire, and want opposite fixes.
  in_container 'cd / && blitzecdn cert list --json' || true
  in_container "openssl x509 -noout -fingerprint -sha256 \
    -in /etc/blitzecdn/tls/${ACME_SITE}/fullchain.pem" || true
  in_container 'ls -la /etc/blitzecdn/tls/'"${ACME_SITE}" || true
  in_container 'docker exec blitzecdn-edge nginx -T 2>/dev/null | grep ssl_certificate' || true
  dump_ansible_log
  fail "the edge serves the same certificate it served before the renewal"
}
in_container "openssl s_client -connect 127.0.0.1:443 -servername ${ACME_DOMAIN} \
  -CAfile /root/pebble/root.pem -verify_return_error -verify_hostname ${ACME_DOMAIN} \
  </dev/null >/dev/null 2>&1" || {
  served_certificate -issuer -ext subjectAltName -enddate || true
  fail "the renewed certificate does not validate against the ACME root"
}
served_certificate -issuer -ext subjectAltName -enddate ||
  fail "could not read back the renewed certificate"

# Removing the record must withdraw the vhost, which is the registry's job.
in_container 'cd / && blitzecdn record remove example.com cdn --yes' ||
  fail "could not remove the record"
deploy_or_fail BLITZE_ALLOW_EMPTY_SITES=true "withdrawing the last site failed"
in_container "test ! -e /etc/blitzecdn/releases/current/nginx/sites-enabled/${ACME_SITE}.conf" ||
  fail "the stale site was left enabled"
in_container 'docker exec blitzecdn-edge nginx -t' ||
  fail "nginx does not load after the site was withdrawn"

say "Backing up the control plane while the API is serving"
in_container 'cd / && blitzecdn backup create' ||
  fail "backup create failed"
in_container 'ls /var/backups/blitzecdn/blitzecdn-backup-*Z.tar.gz >/dev/null' ||
  fail "no backup landed in /var/backups/blitzecdn"
# The expansions belong to the container shell: the archive name is a
# timestamp.
# shellcheck disable=SC2016
in_container 'test "$(stat -c %a "$(ls -t /var/backups/blitzecdn/*.tar.gz | head -1)")" = 600' ||
  fail "the backup archive is not 0600"
# shellcheck disable=SC2016
in_container 'cd / && blitzecdn backup inspect "$(ls -t /var/backups/blitzecdn/*.tar.gz | head -1)" | grep -q "^  database$"' ||
  fail "the backup does not declare a database component"
# /var/backups/blitzecdn is mounted at the same path on host and in the CLI
# container, unlike the state directory, so both halves name the same file.
in_container 'cd / && blitzecdn backup create --only database -o /var/backups/blitzecdn/database-only.tar.gz' ||
  fail "a database-only backup failed"
in_container 'test -s /var/backups/blitzecdn/database-only.tar.gz' || fail "the backup is empty"
in_container 'cd / && blitzecdn backup restore /var/backups/blitzecdn/database-only.tar.gz --yes' ||
  fail "a database-only restore failed"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-api | grep -qx healthy' ||
  fail "the database restore did not restart the API"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-worker | grep -qx healthy' ||
  fail "the database restore did not restart the worker"

say "Restoring configuration and credentials from a full backup"
in_container 'blitzecdn backup create -o /var/backups/blitzecdn/full-restore.tar.gz' ||
  fail "the full backup failed"
in_container 'tar -xOf /var/backups/blitzecdn/full-restore.tar.gz config/env | grep -q BLITZE_API_KEYS=' ||
  fail "the full backup omitted the API credentials"
in_container 'sed -i "s/allow_empty_sites = false/allow_empty_sites = true/" /etc/blitzecdn/blitzecdn.toml' ||
  fail "could not change the configuration before restore"
# shellcheck disable=SC2016
in_container 'printf "BLITZE_API_KEYS=operator:%s\n" "$(openssl rand -hex 32)" > /etc/blitzecdn/blitzecdn.env' ||
  fail "could not change the credential before restore"
in_container 'blitzecdn backup restore /var/backups/blitzecdn/full-restore.tar.gz --yes' ||
  fail "the full restore failed"
in_container 'tar -xOf /var/backups/blitzecdn/full-restore.tar.gz config/env | cmp -s - /etc/blitzecdn/blitzecdn.env' ||
  fail "the full restore did not recover the archived credentials"
in_container 'grep -q "allow_empty_sites = false" /etc/blitzecdn/blitzecdn.toml' ||
  fail "the full restore did not replace the host configuration"
in_container 'docker exec blitzecdn-api python -c "import tomllib; assert not tomllib.load(open(\"/opt/blitzecdn/blitzecdn.toml\", \"rb\"))[\"blitzecdn\"][\"allow_empty_sites\"]"' ||
  fail "the API still sees the old configuration mount"
# shellcheck disable=SC2016
in_container 'test "$(stat -c %a /etc/blitzecdn/blitzecdn.env)" = 600' ||
  fail "restored credentials are not private"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-worker | grep -qx healthy' ||
  fail "the full restore did not recover the worker"
in_container 'docker compose --file /etc/blitzecdn/control-plane.compose.yml ps --status running --services | grep -qx blitzecdn-api' ||
  fail "Compose cannot discover the API recreated through the Docker SDK"
in_container 'docker compose --file /etc/blitzecdn/control-plane.compose.yml ps --status running --services | grep -qx blitzecdn-worker' ||
  fail "Compose cannot discover the worker recreated through the Docker SDK"

say "Repointing the API access list"
# The environment file is written once, so on an existing host the access list
# is converged rather than seeded.
in_container "cd /opt/blitzecdn && ./install.sh standalone --admin-cidr ${ADMIN_CIDR} --email ${ACME_EMAIL} --allowed-ips ${REPOINTED_IPS}" ||
  fail "the installer could not repoint the API access list"
in_container "grep -qx 'BLITZE_ALLOWED_IPS=${REPOINTED_IPS}' /etc/blitzecdn/blitzecdn.env" ||
  fail "the access list was not repointed"
in_container "grep -c '^BLITZE_ALLOWED_IPS=' /etc/blitzecdn/blitzecdn.env | grep -qx 1" ||
  fail "repointing the access list left more than one assignment behind"
# Still the operator's credential, on a run that rewrote a line of their file.
in_container 'grep -q "^BLITZE_API_KEYS=operator:" /etc/blitzecdn/blitzecdn.env' ||
  fail "repointing the access list disturbed the API credential"

say "Re-running the installer"
in_container "cd /opt/blitzecdn && ./install.sh standalone --admin-cidr ${ADMIN_CIDR} --email ${ACME_EMAIL}" ||
  fail "the installer is not re-runnable on ${IMAGE}"
# Omitting the flag leaves the access list exactly as the operator left it.
in_container "grep -qx 'BLITZE_ALLOWED_IPS=${REPOINTED_IPS}' /etc/blitzecdn/blitzecdn.env" ||
  fail "a run without --allowed-ips changed the access list"

say "Checking the updater refuses a checkout it cannot verify"
# The working tree has no .git, so `update` must refuse, and a host serving
# before the refusal still serves after it. The happy path needs a real clone
# and fetch.
in_container 'cd /opt/blitzecdn && ./install.sh update --yes' &&
  fail "update ran against a checkout with no origin"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-api | grep -qx healthy' ||
  fail "a refused update stopped the API"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-worker | grep -qx healthy' ||
  fail "a refused update stopped the worker"

say "Uninstalling"
in_container 'cd /opt/blitzecdn && ./install.sh --uninstall --yes' ||
  fail "uninstall failed on ${IMAGE}"

say "Checking the host is clean"
for path in /opt/blitzecdn /etc/blitzecdn /usr/local/bin/blitzecdn \
  /etc/sudoers.d/blitzecdn-deploy /var/lib/blitzecdn /home/deploy; do
  in_container "test ! -e ${path}" || fail "${path} survived the uninstall"
done
in_container 'getent passwd blitzecdn >/dev/null' && fail "unexpected blitzecdn account appeared"
in_container 'getent group blitzecdn >/dev/null' && fail "unexpected blitzecdn group appeared"
in_container 'getent passwd deploy >/dev/null' && fail "deploy account survived"
in_container 'ls /etc/systemd/system | grep -q blitzecdn' && fail "unit files survived"
in_container 'docker ps --all --format "{{.Names}}" | grep -q "^blitzecdn-\(api\|worker\)$"' &&
  fail "control-plane containers survived"

printf '\nPASS: %s completed install, re-install, and uninstall\n' "${IMAGE}"
