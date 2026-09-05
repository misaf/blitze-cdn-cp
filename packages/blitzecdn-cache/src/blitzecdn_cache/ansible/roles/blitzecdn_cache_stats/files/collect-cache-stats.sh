#!/bin/sh
# Summarize recent BlitzeCDN access-log lines as JSON, for the control plane.
#
# Run through Ansible's script module, which copies this to a temporary
# directory, executes it, and deletes it — the edge keeps no part of it. The
# counting happens here rather than on the controller because a busy edge logs
# gigabytes a day and shipping that back to be counted would cost far more than
# the answer is worth.
#
# Usage: collect-cache-stats.sh <access-log> <max-lines>
set -eu

LOG=${1:?access log path required}
MAX_LINES=${2:?max lines required}

# A log that does not exist yet is an ordinary state: nginx creates it on the
# first request after blitzecdn_nginx enables it. Reporting no requests is
# correct and lets a fresh edge appear in the output as itself, rather than
# failing the whole run and hiding every other edge's numbers.
if [ ! -r "$LOG" ]; then
    printf '[]'
    exit 0
fi

tail -n "$MAX_LINES" -- "$LOG" | awk '
# Reads the `blitzecdn` log_format defined by blitzecdn_nginx:
#
#   $remote_addr $host "$request" $status $body_bytes_sent rt=... cache="HIT" ...
#
# so $2 is the virtual host. The cache outcome is pulled out with match()
# rather than by field number: the request and user-agent fields contain
# spaces, which moves every column after them.
{
    host = $2
    if (host == "" || host == "-") {
        host = "unknown"
    }
    # A request that never consulted the cache logs an empty value — a
    # redirect, an error nginx served itself, or a site with cache_enabled
    # false. Counted as NONE and kept out of the ratio rather than scored as a
    # miss, which would make a working cache look broken on a redirect-heavy
    # site.
    outcome = "NONE"
    if (match($0, /cache="[^"]*"/)) {
        value = substr($0, RSTART + 7, RLENGTH - 8)
        if (value != "" && value != "-") {
            outcome = value
        }
    }
    counts[host SUBSEP outcome]++
}
END {
    printf "["
    first = 1
    for (key in counts) {
        split(key, parts, SUBSEP)
        if (!first) {
            printf ","
        }
        printf "{\"site\":\"%s\",\"outcome\":\"%s\",\"requests\":%d}",
            parts[1], parts[2], counts[key]
        first = 0
    }
    printf "]"
}
'
