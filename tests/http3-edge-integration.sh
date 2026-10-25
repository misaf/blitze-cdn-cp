#!/usr/bin/env bash
# Provision and probe a complete containerised BlitzeCDN edge.
#
# The edge host is a privileged Ubuntu 26.04 container running systemd, with a
# real Docker Engine installed inside it by the roles. The edge container uses
# host networking, so the origin and the HTTP/3 client reach it on the outer
# Docker network as visitors would reach a real edge.
#
# Proves: a fresh install converges and is idempotent; every listener answers
# over HTTP/1.1, HTTP/2 and HTTP/3; GeoIP2, Brotli and Under Attack Mode work; a
# site change reloads without replacing the container; an image upgrade replaces
# it and keeps cache and TLS material; a broken image is rolled back; the stack
# returns after an engine restart; and teardown distinguishes removing the
# runtime from destroying its data.
set -Eeuo pipefail

readonly HOST_IMAGE=ubuntu:26.04
readonly CLIENT_IMAGE='ghcr.io/macbre/curl-http3@sha256:69405c4626512bb553ee426edd7a188c59d93344164516c67822a2ae1f26c444'
readonly MMDB_COMMIT=7e629ca2b7c21ea4f414510caed564c42e2a8d93
readonly MMDB_SHA256=b37601903448683d241af52893c8cbf0fed461e0cdebe0bfaca01891fdeb6db9
readonly EDGE_TAG=blitzecdn-edge:integration
readonly EDGE_TAG_NEXT=blitzecdn-edge:integration-next
readonly EDGE_TAG_BROKEN=blitzecdn-edge:integration-broken
# Where the `blitzecdn_geoip` role reads its database: the runtime contract's
# capability data directory, which core creates and mounts.
readonly GEOIP_DIR=/var/lib/blitzecdn/edge-data/geoip
readonly GEOIP_DB="${GEOIP_DIR}/GeoIP2-Country-Test.mmdb"

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
suffix=$$
network="blitzecdn-http3-${suffix}"
edge="blitzecdn-http3-edge-${suffix}"
origin="blitzecdn-http3-origin-${suffix}"
workdir=$(mktemp -d)

cleanup() {
  # `-v`: the edge host's anonymous volumes go with it. They hold a whole
  # container engine's images, and a runner that keeps them keeps gigabytes.
  docker rm -f -v "${edge}" "${origin}" >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
  rm -rf -- "${workdir}"
}
trap cleanup EXIT

say() { printf '\n=== %s ===\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
in_edge() { docker exec "${edge}" bash -lc "$1"; }
# Core's roles, then each capability's, composed as
# `blitzecdn.core.ansible.roles` composes them at run time.
readonly ROLES_PATH='/workspace/src/blitzecdn/assets/ansible/roles:/workspace/src/blitzecdn/capabilities/cache/ansible/roles:/workspace/src/blitzecdn/capabilities/compression/ansible/roles:/workspace/src/blitzecdn/capabilities/security/geoip/ansible/roles:/workspace/src/blitzecdn/capabilities/hardening/ansible/roles:/workspace/src/blitzecdn/capabilities/http/http3/ansible/roles:/workspace/src/blitzecdn/capabilities/resolver/ansible/roles:/workspace/src/blitzecdn/capabilities/security/ansible/roles'
converge() {
  in_edge "cd /workspace && ANSIBLE_ROLES_PATH=${ROLES_PATH} ansible-playbook -i localhost, tests/integration/http3-edge.yml $*"
}

# The module set comes from `blitzecdn edge image spec`, the same command the
# publishing workflow builds from, so the image probed here is the one operators
# get. Resolved here so the script runs by hand on any host with Docker.
say "Resolving the modules the installed capabilities declare"
command -v uv >/dev/null 2>&1 ||
  fail "uv is not on PATH, and the edge image's module set is resolved with it"
uv sync --frozen --project "${project_dir}" >/dev/null
module_args=()
while IFS= read -r argument; do
  if [[ -n ${argument} ]]; then
    module_args+=(--build-arg "${argument}")
  fi
done < <(uv run --project "${project_dir}" --no-sync blitzecdn edge image spec)
# An empty spec would build an image with no modules and fail much later.
[[ ${#module_args[@]} -gt 0 ]] ||
  fail "blitzecdn edge image spec named no build arguments"
printf '  %s\n' "${module_args[@]}"

say "Building the BlitzeCDN edge image"
docker build "${module_args[@]}" --tag "${EDGE_TAG}" \
  "${project_dir}/src/blitzecdn/assets/docker/edge"

# The image an upgrade moves to: one label on top, so new bytes with the same
# Nginx. blitzecdn_edge_stack pins a digest, so a mere retag would recreate
# nothing.
say "Building the image the upgrade moves to"
printf 'FROM %s\nLABEL org.opencontainers.image.revision=integration-next\n' \
  "${EDGE_TAG}" > "${workdir}/Dockerfile.next"
docker build --tag "${EDGE_TAG_NEXT}" --file "${workdir}/Dockerfile.next" "${workdir}"

# One that cannot serve: its base configuration is broken inside the image,
# which the configuration test must catch before the running container is
# replaced.
say "Building a deliberately broken edge image"
printf 'FROM %s\nRUN printf "this is not nginx configuration\\n" > /etc/nginx/nginx.conf\n' \
  "${EDGE_TAG}" > "${workdir}/Dockerfile.broken"
docker build --tag "${EDGE_TAG_BROKEN}" --file "${workdir}/Dockerfile.broken" "${workdir}"

say "Exporting the images for the edge host's own engine"
docker save "${EDGE_TAG}" "${EDGE_TAG_NEXT}" "${EDGE_TAG_BROKEN}" -o "${workdir}/edge-images.tar"

docker network create "${network}" >/dev/null

say "Starting a compressible origin response"
docker run -d --name "${origin}" --network "${network}" \
  --network-alias blitzecdn-http3-origin python:3.14-alpine \
  sh -c 'mkdir -p /srv && head -c 8192 /dev/zero | tr "\0" x > /srv/index.html && exec python -m http.server 443 -d /srv' \
  >/dev/null

say "Starting a clean Ubuntu 26.04 edge host"
# Anonymous volumes for /var/lib/docker and /var/lib/containerd: the nested
# engine's overlay mounts cannot sit on the outer container's overlay rootfs
# (they fail with a bare `invalid argument`), as with `docker:dind`.
docker run -d --name "${edge}" --hostname blitzecdn-http3-edge \
  --network "${network}" --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --tmpfs /run --tmpfs /run/lock \
  -v /var/lib/docker -v /var/lib/containerd \
  -v "${project_dir}:/workspace:ro" \
  -v "${workdir}:/images:ro" \
  "${HOST_IMAGE}" \
  bash -c 'apt-get update -qq && apt-get install -y -qq systemd systemd-sysv >/dev/null && exec /sbin/init' \
  >/dev/null

state=starting
for _ in $(seq 60); do
  state=$(in_edge 'systemctl is-system-running' 2>/dev/null || true)
  [[ ${state} == running || ${state} == degraded ]] && break
  sleep 2
done
[[ ${state} == running || ${state} == degraded ]] ||
  fail "systemd did not start (last state: ${state})"

say "Installing the minimal Ansible test runner"
in_edge 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ansible-core openssl curl ca-certificates iproute2 >/dev/null'
in_edge 'ansible-galaxy collection install -r /workspace/src/blitzecdn/assets/ansible/requirements.yml >/dev/null'
# Placed by hand under the capability data directory, since this runner has no
# MaxMind credentials; that is also the air-gapped path.
in_edge "install -d -m 0755 ${GEOIP_DIR} && curl -fsSL --proto '=https' --tlsv1.2 'https://raw.githubusercontent.com/maxmind/MaxMind-DB/${MMDB_COMMIT}/test-data/GeoIP2-Country-Test.mmdb' -o ${GEOIP_DB} && echo '${MMDB_SHA256}  ${GEOIP_DB}' | sha256sum -c -"
in_edge "install -d -m 0700 /etc/blitzecdn/tls && openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=site-one.test' -addext 'subjectAltName=DNS:site-one.test,DNS:site-two.test' -keyout /etc/blitzecdn/tls/integration.key -out /etc/blitzecdn/tls/integration.pem >/dev/null 2>&1 && chmod 0600 /etc/blitzecdn/tls/integration.key"
in_edge "openssl rand -base64 48 > /run/blitzecdn-under-attack-secret && chmod 0600 /run/blitzecdn-under-attack-secret"

# The images must reach this host's own engine (the playbook does not pull), and
# `docker load` needs the engine first, so it is installed on its own.
say "Installing the container engine and loading the edge runtime images"
in_edge "cd /workspace && ANSIBLE_ROLES_PATH=${ROLES_PATH} ansible-playbook -i localhost, tests/integration/docker-engine.yml"
in_edge 'docker load -i /images/edge-images.tar'
for tag in "${EDGE_TAG}" "${EDGE_TAG_NEXT}" "${EDGE_TAG_BROKEN}"; do
  in_edge "docker image inspect ${tag} >/dev/null" ||
    fail "${tag} did not reach the edge host's engine"
done

# --------------------------------------------------------------------------
# Fresh installation
# --------------------------------------------------------------------------
say "Converging a fresh Docker edge"
converge

say "Proving the host runs no traffic-serving BlitzeCDN packages"
in_edge '! command -v nginx' || fail "nginx was installed on the edge host"
in_edge '! command -v geoipupdate' || fail "geoipupdate was installed on the edge host"
in_edge 'dpkg-query -W nginx 2>/dev/null && exit 1; exit 0' ||
  fail "the nginx package is installed on the edge host"

say "Proving the runtime is a Compose project"
in_edge 'docker compose --file /etc/blitzecdn/compose.yml ps --format "{{.Service}} {{.State}}"'
in_edge 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-edge | grep -qx healthy'
in_edge 'docker inspect -f "{{.HostConfig.NetworkMode}}" blitzecdn-edge | grep -qx host'
in_edge 'docker exec blitzecdn-edge nginx -t'
in_edge 'docker exec blitzecdn-edge nginx -V 2>&1 | grep -q -- --with-http_v3_module'
in_edge "docker exec blitzecdn-edge nginx -T 2>&1 | grep -q 'geoip2 ${GEOIP_DB}'"

say "Checking every supported public listener"
for port in 80 8080 8880 2052 2082 2086 2095 443 2053 2083 2087 2096 8443; do
  in_edge "ss -H -lnt | grep -q ':${port} '" || fail "nothing is listening on TCP/${port}"
done
in_edge "ss -H -lnu | grep -q ':443 '" || fail "nothing is listening on UDP/443"
in_edge "grep -qx 'tcp|443|any' /etc/blitzecdn/firewall-rules"
in_edge "grep -qx 'udp|443|any' /etc/blitzecdn/firewall-rules"

# `--diff` only on the repeated converge, so it prints exactly what failed to
# settle. Safe: the task rendering the fleet secret is `no_log: true`.
say "Checking provisioning convergence"
converged=$(converge --diff)
printf '%s\n' "${converged}"
grep -Eq 'changed=0[[:space:]]' <<<"${converged}" ||
  fail "a repeated converge reported changes"

container_id=$(in_edge 'docker inspect -f "{{.Id}}" blitzecdn-edge')

# --------------------------------------------------------------------------
# Traffic
# --------------------------------------------------------------------------
edge_ip=$(docker inspect -f "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}" "${edge}")
client=(docker run --rm --network "${network}" "${CLIENT_IMAGE}" curl -kfsS --resolve "site-one.test:443:${edge_ip}")
challenge_client=(docker run --rm --network "${network}" "${CLIENT_IMAGE}" curl -ksS --resolve "site-two.test:443:${edge_ip}")

say "Under Attack Mode intercept before origin processing"
challenge=$("${challenge_client[@]}" --http2 -D - -o /dev/null https://site-two.test/private)
printf '%s\n' "${challenge}"
if ! grep -q 'HTTP/2 302' <<<"${challenge}"; then
  in_edge 'docker logs --tail 50 blitzecdn-edge' || true
fi
grep -q 'HTTP/2 302' <<<"${challenge}"
grep -qi '^x-blitzecdn-mitigation: challenge' <<<"${challenge}"
grep -Eqi '^location: (https://site-two\.test)?/\.blitzecdn/challenge\?token=' <<<"${challenge}"
if grep -qi '^x-cache-status:' <<<"${challenge}"; then
  fail "Under Attack Mode challenge entered the CDN cache flow"
fi

say "HTTP/1.1 request"
http1=$("${client[@]}" --http1.1 -o /dev/null -w 'protocol=%{http_version} code=%{http_code}\n' https://site-one.test/)
printf '%s' "${http1}"
grep -q 'protocol=1.1 code=200' <<<"${http1}"

say "HTTP/2 request"
http2=$("${client[@]}" --http2 -o /dev/null -w 'protocol=%{http_version} code=%{http_code}\n' https://site-one.test/)
printf '%s' "${http2}"
grep -q 'protocol=2 code=200' <<<"${http2}"

say "HTTP/3 request over UDP/443"
http3=$("${client[@]}" --http3-only -D - -o /dev/null -w 'protocol=%{http_version} code=%{http_code}\n' https://site-one.test/)
printf '%s' "${http3}"
grep -q 'protocol=3 code=200' <<<"${http3}"
grep -qi 'alt-svc: h3=":443"; ma=86400' <<<"${http3}"

say "Brotli filter response"
brotli=$("${client[@]}" --http2 -H 'Accept-Encoding: br' -D - -o /dev/null https://site-one.test/)
printf '%s' "${brotli}"
grep -qi 'content-encoding: br' <<<"${brotli}"

# --------------------------------------------------------------------------
# HTTP-01
# --------------------------------------------------------------------------
# The one flow where the controller writes on the host and the runtime reads
# through a bind mount, as the image's uid. Driven through the shipped playbook,
# since the writer and server agreeing is the property.
say "Serving an HTTP-01 challenge the way ACME asks for one"
readonly ACME_TOKEN=blitzecdn-integration-token
readonly ACME_VALIDATION=blitzecdn-integration-token.EhSCDKFbQfBRXBcJ2Vd0
readonly ACME_PLAYBOOK=src/blitzecdn/capabilities/tls/ansible/playbooks/acme-challenge.yml
readonly ACME_URL="http://site-one.test/.well-known/acme-challenge/${ACME_TOKEN}"
# Port 80 and no -f: the code is the assertion, and a CA does not follow
# redirects either.
acme_client=(docker run --rm --network "${network}" "${CLIENT_IMAGE}" curl -sS --resolve "site-one.test:80:${edge_ip}")

publish_challenge() {
  in_edge "cd /workspace && ANSIBLE_ROLES_PATH=${ROLES_PATH} ansible-playbook \
    -i tests/integration/edges-local.ini ${ACME_PLAYBOOK} \
    -e blitzecdn_acme_action=$1 \
    -e blitzecdn_acme_domain=site-one.test \
    -e blitzecdn_acme_token=${ACME_TOKEN} \
    -e blitzecdn_acme_validation=${ACME_VALIDATION}"
}

publish_challenge present || fail "the ACME challenge playbook could not publish a token"

served=$("${acme_client[@]}" -o /dev/null -w '%{http_code}' "${ACME_URL}")
if [[ ${served} != 200 ]]; then
  # Numeric ownership: only the uid crosses the mount.
  in_edge 'ls -lna /var/lib/blitzecdn/acme/.well-known/acme-challenge' || true
  in_edge 'docker exec blitzecdn-edge id' || true
  in_edge 'docker logs --tail 20 blitzecdn-edge' || true
  fail "the edge answered ${served} for a challenge that is on its own disk"
fi

body=$("${acme_client[@]}" "${ACME_URL}")
[[ ${body} == "${ACME_VALIDATION}" ]] ||
  fail "the edge served '${body}' rather than the validation value"

# A token outliving its order could answer the next validation wrongly.
publish_challenge absent || fail "the ACME challenge playbook could not withdraw a token"
withdrawn=$("${acme_client[@]}" -o /dev/null -w '%{http_code}' "${ACME_URL}")
[[ ${withdrawn} == 404 ]] ||
  fail "the withdrawn challenge is still served (${withdrawn})"

# --------------------------------------------------------------------------
# Configuration deployment is a reload, never a replacement
# --------------------------------------------------------------------------
say "A configuration change reloads rather than recreates the container"
in_edge 'install -d -m 0755 /var/cache/nginx/blitzecdn/keep && touch /var/cache/nginx/blitzecdn/keep/marker'
converge -e blitzecdn_nginx_hsts_enabled=true
in_edge "docker inspect -f '{{.Id}}' blitzecdn-edge | grep -qx '${container_id}'" ||
  fail "a configuration change replaced the edge container"
hsts=$("${client[@]}" --http2 -D - -o /dev/null https://site-one.test/)
grep -qi '^strict-transport-security:' <<<"${hsts}" ||
  fail "the reloaded configuration is not in force"

say "An invalid configuration is refused and rolled back"
broken=$(in_edge "printf 'this is not nginx configuration\n' > /etc/nginx/conf.d/zz-broken.conf; docker exec blitzecdn-edge nginx -t 2>&1; rm -f /etc/nginx/conf.d/zz-broken.conf" || true)
grep -qi 'emerg' <<<"${broken}" ||
  fail "the containerised nginx accepted a broken configuration"
in_edge 'docker exec blitzecdn-edge nginx -t'

# --------------------------------------------------------------------------
# Image upgrade
# --------------------------------------------------------------------------
say "Upgrading the edge runtime image"
converge -e blitzecdn_edge_image="${EDGE_TAG_NEXT}"
in_edge "docker inspect -f '{{.Id}}' blitzecdn-edge | grep -qx '${container_id}'" &&
  fail "an image upgrade did not replace the edge container"
in_edge 'docker inspect -f "{{.Config.Image}}" blitzecdn-edge'
in_edge 'test -f /var/cache/nginx/blitzecdn/keep/marker' ||
  fail "the upgrade destroyed the cache"
in_edge 'test -s /etc/blitzecdn/tls/integration.key' ||
  fail "the upgrade destroyed the TLS material"
in_edge 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-edge | grep -qx healthy'
upgraded_id=$(in_edge 'docker inspect -f "{{.Id}}" blitzecdn-edge')

say "Traffic survived the upgrade"
"${client[@]}" --http3-only -o /dev/null -w 'protocol=%{http_version} code=%{http_code}\n' https://site-one.test/ |
  grep -q 'protocol=3 code=200'

# --------------------------------------------------------------------------
# Rollback
# --------------------------------------------------------------------------
say "A broken image is refused and the previous one restored"
rollback=$(converge -e blitzecdn_edge_image="${EDGE_TAG_BROKEN}" 2>&1 || true)
printf '%s\n' "${rollback}"
grep -q 'failed=1' <<<"${rollback}" ||
  fail "converging a broken edge image reported success"
in_edge 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-edge | grep -qx healthy' ||
  fail "the edge did not come back after a failed image upgrade"
"${client[@]}" --http2 -o /dev/null -w 'code=%{http_code}\n' https://site-one.test/ | grep -q 'code=200'
in_edge "grep -q '${EDGE_TAG_NEXT}' /var/lib/blitzecdn/edge/image" ||
  fail "the recorded image is not the one that was rolled back to"

# --------------------------------------------------------------------------
# Restart
# --------------------------------------------------------------------------
say "The stack returns after the engine restarts"
in_edge 'systemctl restart docker'
for _ in $(seq 30); do
  in_edge 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-edge 2>/dev/null | grep -qx healthy' && break
  sleep 2
done
in_edge 'docker inspect -f "{{.State.Health.Status}}" blitzecdn-edge | grep -qx healthy' ||
  fail "the edge did not return after the engine restarted"
"${client[@]}" --http2 -o /dev/null -w 'code=%{http_code}\n' https://site-one.test/ | grep -q 'code=200'
[[ ${upgraded_id} != "" ]]

# --------------------------------------------------------------------------
# Firewall and HTTP/3 stay in step
# --------------------------------------------------------------------------
say "Withdrawing stale UDP/443 firewall state"
in_edge "cd /workspace && ANSIBLE_ROLES_PATH=${ROLES_PATH} ansible-playbook -i localhost, tests/integration/http3-firewall-disabled.yml"
in_edge "grep -qx 'tcp|443|any' /etc/blitzecdn/firewall-rules"
in_edge "! grep -q '^udp|443|any$' /etc/blitzecdn/firewall-rules"

# --------------------------------------------------------------------------
# Teardown, both kinds
# --------------------------------------------------------------------------
say "Removing the runtime without destroying what it served"
in_edge "cd /workspace && ANSIBLE_ROLES_PATH=${ROLES_PATH} ansible-playbook -i localhost, tests/integration/edge-teardown.yml -e blitzecdn_edge_teardown_remove_data=false"
in_edge 'docker ps -a --format "{{.Names}}" | grep -qx blitzecdn-edge' &&
  fail "the edge container survived a runtime teardown"
in_edge 'test -s /etc/blitzecdn/tls/integration.key' ||
  fail "a runtime teardown destroyed the TLS material"
in_edge 'test -d /var/cache/nginx/blitzecdn' ||
  fail "a runtime teardown destroyed the cache"

say "Destroying the persistent state on request"
in_edge "cd /workspace && ANSIBLE_ROLES_PATH=${ROLES_PATH} ansible-playbook -i localhost, tests/integration/edge-teardown.yml"
in_edge '! test -e /etc/blitzecdn' || fail "the state tree survived a destructive teardown"
in_edge '! test -e /var/cache/nginx/blitzecdn' || fail "the cache survived a destructive teardown"
in_edge '! test -e /var/lib/blitzecdn/acme' || fail "the ACME state survived a destructive teardown"

say "Docker edge integration passed"
