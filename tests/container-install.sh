#!/usr/bin/env bash
# Run the whole installer lifecycle against a throwaway container.
#
#   tests/container-install.sh debian:13
#
# This is the only test that exercises what `install.sh standalone` actually
# does. Everything else asserts on the script's shape or runs it in a sandbox
# with the privileged commands stubbed, because provisioning needs root and a
# real init. Here it gets both: systemd as PID 1, a real apt, real accounts.
#
# It also checks the supported fresh-edge platform contract against a real OS.
#
# Requires Docker and about five minutes per image.
set -Eeuo pipefail

readonly IMAGE=${1:?usage: container-install.sh IMAGE}
readonly ADMIN_CIDR=203.0.113.8/32
readonly ACME_EMAIL=ops@example.com
# A CA that speaks ACME, for the issuance stage. Pinned rather than :latest —
# a harness that silently follows someone else's release is one that fails on a
# commit that did not touch it.
readonly PEBBLE_IMAGE=ghcr.io/letsencrypt/pebble:2.10.1
readonly CHALLTESTSRV_IMAGE=ghcr.io/letsencrypt/pebble-challtestsrv:2.10.1
# The site the record below routes to, and the hostname that record publishes —
# the label and the zone, joined. Spelled once because the issuance stage has to
# ask a CA for exactly the name the deploy told the edge to serve.
readonly ACME_SITE=cdn-example-test
readonly ACME_DOMAIN=cdn.example.test

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
container="blitzecdn-$(printf '%s' "${IMAGE}" | tr -c 'a-z0-9' '-')-$$"
archive=$(mktemp -t blitzecdn-source-XXXXXX).tgz
pebble_config=$(mktemp -t blitzecdn-pebble-XXXXXX).json

cleanup() {
  # `-v`: the host's anonymous volumes go with it. They hold a whole container
  # engine's images, and a runner that keeps them keeps gigabytes.
  docker rm -f -v "${container}" >/dev/null 2>&1 || true
  rm -f -- "${archive}" "${pebble_config}"
}
trap cleanup EXIT

say() { printf '\n=== %s ===\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

in_container() { docker exec "${container}" bash -c "$1"; }
# Same, but with stdin attached, for streaming an image into the host's engine.
into_container() { docker exec -i "${container}" bash -c "$1"; }

# Whatever the edge presents for the ACME hostname, through whichever `x509`
# fields the caller asks for. Every certificate assertion below reads the wire
# rather than the store: what the control plane believes it installed and what
# an edge answers with are two facts, and only the second one is the product.
served_certificate() {
  in_container "openssl s_client -connect 127.0.0.1:443 -servername ${ACME_DOMAIN} \
    </dev/null 2>/dev/null | openssl x509 -noout $*"
}

say "Starting ${IMAGE} with systemd"
# `-v /var/lib/docker -v /var/lib/containerd`: anonymous volumes, and the only
# reason this host can run a container at all. The engine installed inside it
# assembles every image as an overlay mount whose upper and lower directories
# live under those paths, and those paths are the *outer* container's rootfs,
# which is itself overlay. Stacking one on the other is refused with a bare
# `invalid argument` about a mount, naming no image and no layer. A volume is
# backed by the outer daemon's own filesystem rather than by that rootfs, which
# is what `docker:dind` does with `VOLUME /var/lib/docker` and for this reason.
# Both paths: this engine keeps its snapshots under containerd's.
docker run -d --name "${container}" \
  --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --tmpfs /run --tmpfs /run/lock \
  -v /var/lib/docker -v /var/lib/containerd \
  "${IMAGE}" \
  bash -c 'apt-get update -qq && apt-get install -y -qq systemd systemd-sysv >/dev/null && exec /sbin/init' \
  >/dev/null

# systemd needs a moment, and how long depends on the image and the runner.
# Poll rather than sleep so a fast host is not punished and a slow one is not
# flaky. "degraded" is accepted: units unrelated to BlitzeCDN routinely fail in
# a container, and refusing to continue would test Docker rather than the
# installer.
for _ in $(seq 60); do
  state=$(in_container 'systemctl is-system-running' 2>/dev/null || true)
  [[ ${state} == running || ${state} == degraded ]] && break
  sleep 2
done
[[ ${state} == running || ${state} == degraded ]] ||
  fail "systemd never came up in ${IMAGE} (last state: ${state:-unknown})"

say "Copying the working tree to /opt/blitzecdn"
# The working tree, not a clone: CI must test the commit under review. Runtime
# state, caches, local configuration and host metadata must not cross this
# boundary. In particular, macOS tar otherwise emits AppleDouble `._*.py`
# sidecars for extended attributes; Alembic treats those binary files as
# revisions and a clean Linux installation fails with a null-byte SyntaxError.
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
# Not /tmp: systemd mounts a tmpfs over it on newer images, which hides
# anything docker cp put there first.
docker cp "${archive}" "${container}:/root/source.tgz" >/dev/null
in_container 'tar -xzf /root/source.tgz -C /opt/blitzecdn' 2>/dev/null

say "Installing"
if ! in_container "cd /opt/blitzecdn && ./install.sh standalone --admin-cidr ${ADMIN_CIDR} --email ${ACME_EMAIL}"; then
  # Preserve the evidence before the EXIT trap removes the disposable host.
  # This catches corrupt source/package copies that otherwise surface only as
  # an opaque import error several layers inside the installer.
  # The expansions belong to the container shell.
  # shellcheck disable=SC2016
  in_container 'for path in /opt/blitzecdn/src/blitzecdn/migrations/versions/0001_initial_schema.py /opt/blitzecdn/.venv/lib/python3.13/site-packages/blitzecdn/migrations/versions/0001_initial_schema.py; do printf "%s bytes=" "$path"; wc -c < "$path"; done' || true
  in_container 'sha256sum /opt/blitzecdn/src/blitzecdn/migrations/versions/0001_initial_schema.py /opt/blitzecdn/.venv/lib/python3.13/site-packages/blitzecdn/migrations/versions/0001_initial_schema.py' || true
  fail "install failed on ${IMAGE}"
fi

say "Checking what the installation produced"
in_container '! getent passwd blitzecdn >/dev/null' || fail "unexpected blitzecdn host account"
in_container '! getent group blitzecdn >/dev/null' || fail "unexpected blitzecdn host group"
in_container 'getent passwd deploy >/dev/null' || fail "no deploy account"
in_container 'test -x /usr/local/bin/blitzecdn' || fail "no CLI wrapper"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-api | grep -qx healthy' || {
  in_container 'docker compose --file /etc/blitzecdn/control-plane.compose.yml ps' || true
  in_container 'docker compose --file /etc/blitzecdn/control-plane.compose.yml logs blitzecdn-api' || true
  fail "API not running"
}
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-redis | grep -qx healthy' || fail "Redis not running"
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
in_container 'stat -c %U:%G:%a /opt/blitzecdn/blitzecdn.toml | grep -qx root:root:644' ||
  fail "non-secret configuration is not host-managed"
in_container 'docker exec blitzecdn-api python -c "import sqlite3; p=\"/opt/blitzecdn/.state/permission-test.db\"; c=sqlite3.connect(p); assert c.execute(\"PRAGMA journal_mode=WAL\").fetchone()[0] == \"wal\"; c.execute(\"CREATE TABLE writable (id INTEGER)\"); c.commit(); c.close()"' ||
  fail "SQLite could not create and write WAL state as the application identity"
in_container 'test -f /var/lib/blitzecdn/permission-test.db && rm -f /var/lib/blitzecdn/permission-test.db*' ||
  fail "SQLite permission test did not write into persistent state"
# From / rather than the checkout: the wrapper exists to make that work.
in_container 'cd / && blitzecdn --version >/dev/null' || fail "CLI unusable outside the checkout"
in_container 'cd / && blitzecdn doctor --json >/dev/null' || fail "doctor failed"

say "Seeding the edge runtime image this host will run"
# The edge is a container now, so converging one needs an image before the
# first deploy — and CI must test the commit under review rather than a
# published image that lags it by definition. So: build it here, and load it
# into the disposable host's own engine.
#
# Which needs an engine, before the converge that installs one. The repository
# is therefore seeded exactly as blitzecdn_docker seeds it, into the same
# deb822 file the role writes, so the converge that follows still finds
# everything as it expects and reports no change on its second run. This is the
# only step in this script that pre-empts a role, and it does so with that
# role's own configuration.
docker build --quiet --tag blitzecdn-edge:standalone "${project_dir}/src/blitzecdn/docker/edge" >/dev/null

in_container 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl gnupg >/dev/null' ||
  fail "could not install the Docker repository prerequisites"
in_container 'install -d -m 0755 /etc/apt/keyrings && curl -fsSL --proto "=https" --tlsv1.2 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc && chmod 0644 /etc/apt/keyrings/docker.asc' ||
  fail "could not fetch the Docker signing key"
# shellcheck disable=SC2016
in_container 'printf "Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n" "$(. /etc/os-release && printf %s "${VERSION_CODENAME}")" "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/docker.sources' ||
  fail "could not configure the Docker repository"
in_container 'DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null' ||
  fail "could not install Docker Engine"
in_container 'systemctl enable --now docker' || fail "Docker did not start"
docker save blitzecdn-edge:standalone | into_container 'docker load' >/dev/null ||
  fail "could not load the edge runtime image into the disposable host"

# The raw Ansible output for the last run, which is where a check-mode diff
# lands: the runner invokes `--check --diff`, so the log says which line of
# which file the fleet disagrees about.
#
# On the *host*, not in the container. `/opt/blitzecdn/.state` is where the
# control plane sees its state from inside the CLI container; the bind mount
# behind it is `/var/lib/blitzecdn`, and that is the only path a `docker exec`
# into the systemd host can read. Looking in the container's path printed "no
# such file" and nothing else on the one failure it exists for.
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
    grep -B 40 -A 1 "^changed: \[" "$latest" ||
      printf "No changed tasks in the log; the disagreement is not a task diff.\n"
    printf "\n--- recap ---\n"
    sed -n "/^PLAY RECAP/,\$p" "$latest"' || true
}

say "Converging this host as an edge"
# The one place an edge role is ever executed. Everything else about the roles
# is checked by shape — ansible-lint, --syntax-check, the argument-spec
# contract test — and none of that runs a task. So the nginx block/rescue, the
# managed-site registry that drives stale removal, the `not ansible_check_mode`
# gates and the package retries were all unexercised: a role could be rewritten
# into something that cannot converge and every gate would stay green.
#
# The post-bootstrap CLI handoff records edge-local; this verifies that handoff
# rather than creating a second inventory row for the same machine.
in_container 'cd / && blitzecdn edge list --json | grep -q '"'"'"name": "edge-local"'"'"'' ||
  fail "the installer did not register the local edge"
# Fleet policy, set the way an operator sets it: the image an edge runs is a
# database-backed setting the inventory plugin publishes, not desired state.
in_container 'cd / && blitzecdn config set blitzecdn_edge_image blitzecdn-edge:standalone' ||
  fail "could not pin the edge runtime image"
in_container 'cd / && blitzecdn config set blitzecdn_edge_stack_image_pull false' ||
  fail "could not disable the registry pull"

in_container 'cd / && blitzecdn domain add example.test' || fail "could not add a zone"
# The site first, then the record that routes a hostname to it. A record is
# proxied exactly when it names a site — there is no `--proxied` switch to set,
# because turning the proxy off means saying what DNS should answer with
# instead. The site name is what the edge writes its virtual host as, which is
# why the assertions below look for `${ACME_SITE}`.
in_container "cd / && blitzecdn site create ${ACME_SITE} --origin 127.0.0.1" ||
  fail "could not create the site the record routes to"
in_container "cd / && blitzecdn record add example.test cdn --site ${ACME_SITE}" ||
  fail "could not route the hostname to the site"

# Check mode first: it must survive a host that has never converged, which is
# the case the `not ansible_check_mode` gates exist for.
in_container 'cd / && blitzecdn plan --json >/dev/null' || {
  # shellcheck disable=SC2016
  dump_ansible_log
  fail "check-mode run failed"
}

in_container 'cd / && blitzecdn deploy --yes --json >/dev/null' || fail "deploy failed"
in_container "test -f /etc/nginx/sites-enabled/${ACME_SITE}.conf" ||
  fail "the deploy did not enable the managed site"
in_container 'docker exec blitzecdn-edge nginx -t' ||
  fail "the converged nginx configuration does not load"
in_container "grep -q '^${ACME_SITE}\$' /etc/nginx/blitzecdn-managed-sites" ||
  fail "the managed-site registry was not written"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-edge | grep -qx healthy' ||
  fail "the edge container is not healthy"
# The host must be left with no traffic-serving BlitzeCDN runtime packages of
# its own: a host process would compete with the container for public ports.
in_container '! command -v nginx' || fail "nginx was installed on the host"

# Converging twice must change nothing: the drift check is the assertion.
in_container 'cd / && blitzecdn deploy --yes --json >/dev/null' || fail "second deploy failed"
in_container 'cd / && blitzecdn drift --json' || {
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
# The control plane's half of ACME, which nothing else runs. The edge's half —
# serving a challenge something else wrote — belongs to the HTTP/3 harness;
# this is certbot, the manual hooks, the playbook those hooks drive, and the
# chain the store validates and installs, end to end against a CA that speaks
# the protocol.
#
# Pebble rather than Let's Encrypt: issuance has to run against something that
# implements ACME rather than a stub, and it must need no public domain, no
# public IP and nobody's rate limit. Pebble mints a throwaway CA per run and
# validates over HTTP-01 like the real thing.
#
# Preflight is the one part deliberately not exercised: it asks public DNS
# whether the name points at this edge, and example.test is not public. The
# request below skips it, and preflight has its own tests against its own
# resolver.

# Asserted rather than assumed. Everything below would otherwise fail as an
# opaque ACME timeout if the record and the site stopped agreeing on the
# hostname this expects.
in_container "grep -q 'server_name ${ACME_DOMAIN};' /etc/nginx/sites-enabled/${ACME_SITE}.conf" ||
  fail "the deployed site does not serve ${ACME_DOMAIN}"

# Pulled out here and streamed in, the way the edge image is: the disposable
# host has an engine but no reason to hold registry credentials, and the runner
# has already paid for the pull.
docker pull --quiet "${PEBBLE_IMAGE}" >/dev/null || fail "could not pull ${PEBBLE_IMAGE}"
docker pull --quiet "${CHALLTESTSRV_IMAGE}" >/dev/null ||
  fail "could not pull ${CHALLTESTSRV_IMAGE}"
docker save "${PEBBLE_IMAGE}" "${CHALLTESTSRV_IMAGE}" | into_container 'docker load' >/dev/null ||
  fail "could not load the ACME server images into the disposable host"

# Pebble's own API certificate. Its image ships a binary and nothing else — no
# config and no test certificates — so both are supplied here, which also means
# this stage depends on no path inside somebody else's image.
in_container 'mkdir -p /root/pebble && openssl req -x509 -newkey rsa:2048 -sha256 -nodes \
  -days 1 -keyout /root/pebble/key.pem -out /root/pebble/cert.pem -subj /CN=localhost \
  -addext subjectAltName=IP:127.0.0.1,DNS:localhost 2>/dev/null' ||
  fail "could not generate the ACME server certificate"
# `httpPort` 80 is the point of the stage: Pebble validates against the edge's
# real listener rather than a side channel a test controls.
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

# Every name resolves here, so Pebble looks for the challenge where the edge is
# actually serving. Its own challenge responders are switched off: answering
# HTTP-01 is the edge's job and the thing under test.
# On 127.0.0.1:53, not the default :8053, because the control plane's
# `preflight_dns_servers` is a list of addresses and dnspython gives them all
# one port. The loopback address specifically: systemd-resolved holds
# 127.0.0.53:53 in this host, and binding the wildcard would collide with it.
in_container "docker run -d --name pebble-dns --network host ${CHALLTESTSRV_IMAGE} \
  -dnsserver 127.0.0.1:53 -http01 '' -https01 '' -tlsalpn01 ''" >/dev/null ||
  fail "could not start the challenge DNS server"
# `docker run -d` succeeds for a container that starts and immediately dies, so
# it says nothing about whether the server is answering — an unknown flag or a
# taken port both look like a clean start from here, and the next thing to
# notice would be a challenge that fails for no stated reason. Ask it the
# question the CA and preflight will both ask.
in_container 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dnsutils >/dev/null' ||
  fail "could not install a DNS client"
resolved=$(in_container "dig +short +time=2 +tries=2 @127.0.0.1 ${ACME_DOMAIN}") ||
  fail "the challenge DNS server did not answer"
[[ ${resolved} == "127.0.0.1" ]] || {
  in_container 'docker logs pebble-dns' || true
  in_container 'docker inspect -f "{{.State.Running}} {{.State.ExitCode}}" pebble-dns' || true
  fail "the challenge DNS server answered '${resolved}' for ${ACME_DOMAIN}"
}
# NOSLEEP and NONCEREJECT: Pebble's defaults inject a random validation delay
# and reject one nonce in twenty on purpose, to shake out client bugs. Neither
# is what this stage is asking about, and both make it slower and flakier.
# AUTHZREUSE, because the default reuses an authorization half the time: the
# renewal below would then revalidate over HTTP-01 on some runs and skip
# straight to issuance on others, which is coverage decided by a coin toss.
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

# `certbot` is this capability's own setting and it names an executable, so
# pointing it at a wrapper is all it takes to reach a different CA. The file is
# in the image because the control-plane image is built from the checkout.
in_container 'printf "certbot = \"/opt/blitzecdn/tests/integration/certbot-pebble\"\n" \
  >> /opt/blitzecdn/blitzecdn.toml' ||
  fail "could not point the control plane at the test certbot"
# And preflight asks *public* DNS whether the hostname points at an edge, which
# for example.test it does not. Pointing it at the same server the CA uses is
# what lets renewal run at all: renewal goes through preflight and, unlike the
# request endpoint, has no override — an unattended timer must not be able to
# force past a failed check. Replaced rather than appended: this key is already
# in the file, and a second one is a TOML parse error, not an override.
in_container 'sed -i "s|^preflight_dns_servers = .*|preflight_dns_servers = [\"127.0.0.1\"]|" \
  /opt/blitzecdn/blitzecdn.toml' ||
  fail "could not point preflight at the challenge DNS server"
in_container 'grep -q "^preflight_dns_servers = \[\"127.0.0.1\"\]$" /opt/blitzecdn/blitzecdn.toml' ||
  fail "the preflight resolver was not rewritten"
# Configuration is read once at start, so the running processes have to be
# replaced to see it. `--force-recreate` because nothing in the Compose file
# changed: without it `up` finds both services already up-to-date, reports
# `Running`, and leaves the old processes — and their old configuration — in
# place. That is not hypothetical. It is what this stage did on its first run,
# and the certificate request went to the real Let's Encrypt, which refused the
# harness's example.com address; a rejected email was the only sign that the
# CA under test had never been consulted.
started_before=$(in_container 'docker inspect -f "{{.State.StartedAt}}" blitzecdn-api')
in_container 'docker compose --file /etc/blitzecdn/control-plane.compose.yml \
  up --detach --force-recreate --wait --wait-timeout 180 blitzecdn-api blitzecdn-worker' >/dev/null || {
  in_container 'docker compose --file /etc/blitzecdn/control-plane.compose.yml logs blitzecdn-api' || true
  fail "the control plane did not come back with the test certbot configured"
}
# The assertion the comment above is making. A restart that silently did
# nothing is invisible here otherwise: everything downstream still runs, just
# against the wrong CA.
started_after=$(in_container 'docker inspect -f "{{.State.StartedAt}}" blitzecdn-api')
[[ ${started_before} != "${started_after}" ]] ||
  fail "the API was not replaced, so it is still running the old configuration"

# Issuance is an API operation; there is no CLI command for it. The key is read
# inside the container and never crosses into this shell, where it would end up
# in a log the moment anything printed a command.
# shellcheck disable=SC2016
issued=$(in_container 'key=$(sed -n "s/^BLITZE_API_KEYS=operator://p" /etc/blitzecdn/blitzecdn.env)
  [ -n "${key}" ] || { printf "no operator API key in blitzecdn.env\n" >&2; exit 1; }
  curl -sS --max-time 600 -X POST -H "X-API-Key: ${key}" -H "Content-Type: application/json" \
    -d "{\"skip_preflight\": true}" \
    http://127.0.0.1:8000/v1/sites/'"${ACME_SITE}"'/certificate/request') || {
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

# Having a certificate is not the same as serving on 443. `ssl_mode` is the
# switch, it defaults to off, and the site invariant refuses to turn it on
# without an active certificate — so this is the operator's real order, and
# doing it the other way round is refused rather than half-applied. Flexible
# because this site's origin speaks plain HTTP; the modes above it would have
# the edge open TLS to an origin that has none.
in_container "cd / && blitzecdn site ssl ${ACME_SITE} --mode flexible --json >/dev/null" ||
  fail "could not turn on TLS for the site that now has a certificate"

# The certificate exists in the control plane; a deploy is what puts it on the
# edge. Until this runs the site is still being served over HTTP only.
in_container 'cd / && blitzecdn deploy --yes --json >/dev/null' ||
  fail "the deploy that installs the certificate failed"

in_container 'curl -sSk --max-time 5 https://127.0.0.1:15000/roots/0 -o /root/pebble/root.pem' ||
  fail "could not fetch the ACME root"
# The whole chain, checked the way a client checks it: the certificate the edge
# presents for this name has to verify to the root Pebble issued it from, and
# has to be valid *for that name*.
#
# The handshake and not a request, deliberately. This site's origin is the same
# host, so an HTTPS request would proxy to the edge's own HTTP listener and be
# redirected back to itself; what that would measure is a loop, not a chain.
in_container "openssl s_client -connect 127.0.0.1:443 -servername ${ACME_DOMAIN} \
  -CAfile /root/pebble/root.pem -verify_return_error -verify_hostname ${ACME_DOMAIN} \
  </dev/null >/dev/null 2>&1" || {
  in_container "openssl s_client -connect 127.0.0.1:443 -servername ${ACME_DOMAIN} \
    </dev/null 2>/dev/null | openssl x509 -noout -issuer -subject -dates" || true
  # Whether anything is listening at all, and with which files. An empty
  # certificate above is either no HTTPS server block for this name or no
  # handshake, and these two say which.
  in_container "grep -E 'listen|ssl_certificate' /etc/nginx/sites-enabled/${ACME_SITE}.conf" || true
  in_container 'cd / && blitzecdn cert list --json' || true
  in_container 'docker logs --tail 40 pebble' || true
  fail "the edge is not serving a certificate that validates against the ACME root"
}
# What was actually served, in the log. This stage is otherwise silent when it
# passes, which leaves a reader with two banners and an elapsed time as the
# only evidence that a CA was ever involved.
#
# The SAN and not the subject: Pebble issues with `promote CN=false`, so the
# leaf carries no common name at all and the hostname is only in the extension.
# Printing the subject showed an empty line, which reads like a fault and is
# not one — `-verify_hostname` above is what actually holds the name to
# account.
served_certificate -issuer -ext subjectAltName -enddate ||
  fail "could not read back the certificate that just verified"


say "Renewing that certificate the way the timer does"
# Renewal is the unattended half of ACME, and it is not issuance with a flag on
# it: it chooses its own candidates, goes through preflight with no override —
# a timer must not be able to force past a failed check — and has to leave the
# edge serving something other than what it served a moment ago.
#
# Preflight first, and asserted rather than assumed. It is the gate renewal
# cannot bypass, so a failure there arrives as a renewal that reports the site
# as skipped, exits zero, and proves nothing. This is also the only place
# preflight runs for real: the request above skipped it, because until the
# resolver was pointed at the challenge server there was no public DNS in which
# example.test pointed anywhere.
in_container "cd / && blitzecdn cert preflight ${ACME_SITE}" ||
  fail "preflight blocks issuance for a site the edge is already serving"

renewed_from=$(served_certificate -fingerprint -sha256) ||
  fail "could not read the certificate the edge is serving"

# --force because the certificate was issued minutes ago and nothing is due.
# --deploy because reaching the edge is the half the timer owns, and a renewal
# that stops in the store is a certificate that expires anyway.
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
in_container 'cd / && blitzecdn record remove example.test cdn --yes' ||
  fail "could not remove the record"
in_container 'cd / && BLITZE_ALLOW_EMPTY_SITES=true blitzecdn deploy --yes --json >/dev/null' ||
  fail "withdrawing the last site failed"
in_container "test ! -e /etc/nginx/sites-enabled/${ACME_SITE}.conf" ||
  fail "the stale site was left enabled"
in_container 'docker exec blitzecdn-edge nginx -t' ||
  fail "nginx does not load after the site was withdrawn"

say "Backing up the control plane while the API is serving"
in_container 'cd / && blitzecdn backup create' ||
  fail "backup create failed"
in_container 'ls /var/backups/blitzecdn/blitzecdn-backup-*Z.tar.gz >/dev/null' ||
  fail "no backup landed in /var/backups/blitzecdn"
# The expansions belong to the container shell: the archive is named for the
# moment it was taken, so the test cannot know its name in advance.
# shellcheck disable=SC2016
in_container 'test "$(stat -c %a "$(ls -t /var/backups/blitzecdn/*.tar.gz | head -1)")" = 600' ||
  fail "the backup archive is not 0600"
# shellcheck disable=SC2016
in_container 'cd / && blitzecdn backup inspect "$(ls -t /var/backups/blitzecdn/*.tar.gz | head -1)" | grep -q "^  database$"' ||
  fail "the backup does not declare a database component"
# /var/backups/blitzecdn, and not the state directory, because the CLI runs
# inside the control plane's container and the two do not agree on a name for
# the state directory: the host calls it /var/lib/blitzecdn and the container
# mounts it at /opt/blitzecdn/.state. The backup directory is mounted at the
# path it already has, so it is the one place an operator can name an archive
# and have both halves of this test mean the same file.
in_container 'cd / && blitzecdn backup create --only database -o /var/backups/blitzecdn/database-only.tar.gz' ||
  fail "a database-only backup failed"
in_container 'test -s /var/backups/blitzecdn/database-only.tar.gz' || fail "the backup is empty"
in_container 'cd / && blitzecdn backup restore /var/backups/blitzecdn/database-only.tar.gz --yes' ||
  fail "a database-only restore failed"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-api | grep -qx healthy' ||
  fail "the database restore did not restart the API"
in_container 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-worker | grep -qx healthy' ||
  fail "the database restore did not restart the worker"

say "Re-running the installer"
in_container "cd /opt/blitzecdn && ./install.sh standalone --admin-cidr ${ADMIN_CIDR} --email ${ACME_EMAIL}" ||
  fail "the installer is not re-runnable on ${IMAGE}"

say "Checking the updater refuses a checkout it cannot verify"
# The working tree is copied in here rather than cloned, so this host has no
# .git and `update` must refuse rather than run against an unknown source. The
# happy path needs a real clone and a network fetch, which would test origin
# rather than the commit under review; what this pins is that the refusal is
# clean — a host that was serving before a refused update is still serving
# after it, because nothing was stopped on the way to the refusal.
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
in_container 'docker ps --all --format "{{.Names}}" | grep -q "^blitzecdn-\(api\|worker\|redis\)$"' &&
  fail "control-plane containers survived"
in_container 'docker volume inspect blitzecdn-redis >/dev/null 2>&1' &&
  fail "control-plane Redis volume survived"

printf '\nPASS: %s completed install, re-install, and uninstall\n' "${IMAGE}"
