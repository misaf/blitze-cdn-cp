# 0005: Rewrite origin responses on a loopback hop before the cache reads them

Status: Accepted

## Context

Some per-site settings, such as edge TTL overrides and ignored origin
headers, change how the cache treats an origin's response. nginx reads an
entry's lifetime and exclusions with directives that take no variables
(`proxy_cache_valid`, `proxy_ignore_headers`), so they cannot vary per site
inside one shared cache configuration. The only per-site, per-response lever
is the response itself.

## Decision

A capability that needs that lever (today, cache) contributes an `origin` Nginx fragment.
When any such fragment renders for an origin, the site's listeners proxy to a
loopback server on `blitzecdn_nginx_origin_hop_port` instead of the origin.
That hop server carries the fragments and rewrites the origin's headers
before the cache, one hop up, reads them. A site with no such fragment is
proxied straight to its origin.

The [origin connections](../design/origin-connections.md#the-origin-hop)
design note covers how the hop is addressed and what it does or skips
compared with the visitor-facing server.

## Consequences

- Capabilities can add per-site cache behaviour without core changes and
  without one Nginx `server` block per policy combination.
- Sites that use no origin fragment pay nothing.
- A site that does pays one extra loopback proxy per cache miss. Buffering,
  body limits, logging and accounting are done once, on the visitor-facing
  server, not repeated on the hop.
- Connection limits are edge-wide, and every hop request comes from
  127.0.0.1, so the hop runs `limit_conn` in dry-run mode. Visitors are still
  limited where they connect.
