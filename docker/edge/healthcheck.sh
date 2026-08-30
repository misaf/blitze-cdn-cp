#!/bin/sh
# Is this edge actually serving?
#
# Three questions, cheapest first. A container that is merely "running" tells
# nobody anything: Nginx keeps the configuration it loaded at start, so a master
# process can be alive and healthy while the configuration on disk is broken and
# the next reload or restart would fail.
#
#   1. the configuration on disk still parses      -> nginx -t
#   2. the master process is alive                 -> kill -0
#   3. it answers a request on the loopback probe  -> curl
#
# Step 3 is skipped when the status endpoint is disabled, because there is then
# no unauthenticated local endpoint to ask and the alternative — probing a
# customer's virtual host — would put health checks in the access log and in the
# cache. BLITZECDN_HEALTH_URL is set by the Compose file from the same variables
# that render the status server block.
set -eu

nginx -t -q

pid_file=${BLITZECDN_NGINX_PID:-/run/nginx.pid}
[ -s "${pid_file}" ] || { echo "no nginx pid file at ${pid_file}" >&2; exit 1; }
kill -0 "$(cat "${pid_file}")" 2>/dev/null || { echo "nginx master is not running" >&2; exit 1; }

url=${BLITZECDN_HEALTH_URL:-}
[ -n "${url}" ] || exit 0
curl --fail --silent --show-error --max-time 3 --output /dev/null "${url}"
