#!/usr/bin/env bash
# Provision and probe a complete BlitzeCDN edge on a clean Ubuntu 26.04 host.
set -Eeuo pipefail

readonly EDGE_IMAGE=ubuntu:26.04
readonly CLIENT_IMAGE='ghcr.io/macbre/curl-http3@sha256:69405c4626512bb553ee426edd7a188c59d93344164516c67822a2ae1f26c444'
readonly MMDB_COMMIT=7e629ca2b7c21ea4f414510caed564c42e2a8d93
readonly MMDB_SHA256=b37601903448683d241af52893c8cbf0fed461e0cdebe0bfaca01891fdeb6db9

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
suffix=$$
network="blitzecdn-http3-${suffix}"
edge="blitzecdn-http3-edge-${suffix}"
origin="blitzecdn-http3-origin-${suffix}"

cleanup() {
  docker rm -f "${edge}" "${origin}" >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

say() { printf '\n=== %s ===\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
in_edge() { docker exec "${edge}" bash -lc "$1"; }

docker network create "${network}" >/dev/null

say "Starting a compressible origin response"
docker run -d --name "${origin}" --network "${network}" \
  --network-alias blitzecdn-http3-origin python:3.14-alpine \
  sh -c 'mkdir -p /srv && head -c 8192 /dev/zero | tr "\0" x > /srv/index.html && exec python -m http.server 443 -d /srv' \
  >/dev/null

say "Starting a clean Ubuntu 26.04 edge"
docker run -d --name "${edge}" --hostname blitzecdn-http3-edge \
  --network "${network}" --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --tmpfs /run --tmpfs /run/lock \
  -v "${project_dir}:/workspace:ro" \
  "${EDGE_IMAGE}" \
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
in_edge 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ansible-core openssl curl ca-certificates >/dev/null'
in_edge 'ansible-galaxy collection install -r /workspace/ansible/requirements.yml >/dev/null'
in_edge "install -d -m 0755 /usr/share/GeoIP && curl -fsSL --proto '=https' --tlsv1.2 'https://raw.githubusercontent.com/maxmind/MaxMind-DB/${MMDB_COMMIT}/test-data/GeoIP2-Country-Test.mmdb' -o /usr/share/GeoIP/GeoIP2-Country-Test.mmdb && echo '${MMDB_SHA256}  /usr/share/GeoIP/GeoIP2-Country-Test.mmdb' | sha256sum -c -"
in_edge "openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=site-one.test' -addext 'subjectAltName=DNS:site-one.test,DNS:site-two.test' -keyout /etc/ssl/private/blitzecdn-http3-test.key -out /etc/ssl/certs/blitzecdn-http3-test.pem >/dev/null 2>&1"
in_edge "openssl rand -base64 48 > /run/blitzecdn-under-attack-secret && chmod 0600 /run/blitzecdn-under-attack-secret"

say "Running the real BlitzeCDN Nginx role"
in_edge "cd /workspace && ANSIBLE_ROLES_PATH=/workspace/ansible/roles ansible-playbook -i localhost, tests/integration/http3-edge.yml"

say "Checking provisioning convergence"
converge=$(in_edge "cd /workspace && ANSIBLE_ROLES_PATH=/workspace/ansible/roles ansible-playbook -i localhost, tests/integration/http3-edge.yml")
printf '%s\n' "${converge}"
grep -Eq 'changed=0[[:space:]]' <<<"${converge}"

say "Proving package and build capabilities"
in_edge "dpkg-query -W -f='\${Package}=\${Version}\\n' nginx libnginx-mod-http-geoip2 libnginx-mod-http-brotli-filter libnginx-mod-http-js"
in_edge 'nginx -v'
in_edge 'nginx -V'
in_edge 'nginx -t'
in_edge "test \"\$(grep -RhF 'listen 443 quic reuseport' /etc/nginx/sites-enabled | wc -l)\" -eq 1"
in_edge "test \"\$(grep -RhF 'listen 443 quic;' /etc/nginx/sites-enabled | wc -l)\" -eq 2"
in_edge "nginx -T 2>&1 | grep -q 'geoip2 /usr/share/GeoIP/GeoIP2-Country-Test.mmdb'"
in_edge "grep -qx 'tcp|443|any' /etc/blitzecdn/firewall-rules"
in_edge "grep -qx 'udp|443|any' /etc/blitzecdn/firewall-rules"

edge_ip=$(docker inspect -f "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}" "${edge}")
client=(docker run --rm --network "${network}" "${CLIENT_IMAGE}" curl -kfsS --resolve "site-one.test:443:${edge_ip}")
challenge_client=(docker run --rm --network "${network}" "${CLIENT_IMAGE}" curl -ksS --resolve "site-two.test:443:${edge_ip}")

say "Under Attack Mode intercept before origin processing"
challenge=$("${challenge_client[@]}" --http2 -D - -o /dev/null https://site-two.test/private)
printf '%s\n' "${challenge}"
if ! grep -q 'HTTP/2 302' <<<"${challenge}"; then
  in_edge "journalctl -u nginx --no-pager -n 50" || true
fi
grep -q 'HTTP/2 302' <<<"${challenge}"
grep -qi '^x-blitzecdn-mitigation: challenge' <<<"${challenge}"
grep -Eqi '^location: (https://site-two\.test)?/\.blitzecdn/challenge\?token=' <<<"${challenge}"
if grep -qi '^x-cache-status:' <<<"${challenge}"; then
  fail "Under Attack Mode challenge entered the CDN cache flow"
fi

say "Checking module failure paths before configuration apply"
for module in ngx_http_brotli_filter_module.so ngx_http_geoip2_module.so ngx_http_js_module.so; do
  failure=$(in_edge "mv /usr/lib/nginx/modules/${module} /usr/lib/nginx/modules/${module}.disabled; cd /workspace; set +e; ANSIBLE_ROLES_PATH=/workspace/ansible/roles ansible-playbook -i localhost, tests/integration/http3-edge.yml 2>&1; rc=\$?; mv /usr/lib/nginx/modules/${module}.disabled /usr/lib/nginx/modules/${module}; test \${rc} -ne 0")
  printf '%s\n' "${failure}"
  grep -q 'No edge configuration was applied' <<<"${failure}"
done
in_edge 'nginx -t'

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

say "Withdrawing stale UDP/443 firewall state"
in_edge "cd /workspace && ANSIBLE_ROLES_PATH=/workspace/ansible/roles ansible-playbook -i localhost, tests/integration/http3-firewall-disabled.yml"
in_edge "grep -qx 'tcp|443|any' /etc/blitzecdn/firewall-rules"
in_edge "! grep -q '^udp|443|any$' /etc/blitzecdn/firewall-rules"

say "HTTP/3 edge integration passed"
