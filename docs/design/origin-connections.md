# Origin connections

Code: `assets/ansible/roles/blitzecdn_nginx/templates/site.conf.j2`, the
`blitzecdn_nginx_upstream_*` and `blitzecdn_nginx_origin_hop_port` defaults.

## Which origin a listener reaches

`origin_of(listener_port, visitor_scheme)` returns `"<scheme>:<port>"` and is
the one place the SSL-mode matrix is spelled. The upstream groups and the
`proxy_pass` that names them both read it, so they cannot disagree.

* The edge keeps the visitor's destination port: a request on `:8080` is
  fetched from `origin:8080`.
* `full` and `full_strict` speak TLS to the origin on an HTTPS listener.
* `flexible` on 443 terminates TLS at the edge and reaches the HTTP origin on
  port 80. Flexible on any other HTTPS port still speaks TLS to the origin.

## Upstream groups and connection reuse

One `upstream` group per origin connection a site actually proxies to, named
`blitzecdn_origin_<site>_<scheme>_<port>`. Site names are `[a-z0-9-]`, so the
underscores keep group names unique across the edge.

* **Why groups.** A literal `proxy_pass host:port`, or a variable one, has no
  pool: nginx closes the connection when the response ends, and every cache
  miss pays a TCP handshake (and a TLS one for an HTTPS origin). A group keeps
  `blitzecdn_nginx_upstream_keepalive` idle connections per worker for reuse.
  That is a cap on idle sockets, not on concurrency.
* **Keyed by origin, not listener.** Flexible's 443 and the plain 80 listener
  reach the same `http:80` and must not declare it twice.
* **No group without traffic.** A redirecting HTTP listener proxies nothing
  and contributes no group; a site with no hostnames renders no servers and no
  groups. Without resolvers a group's name is resolved at load, and a site
  that serves nothing must not be able to fail the edge's configuration test.
* **Keepalive needs HTTP/1.1 and an empty `Connection` header.** `proxy()`
  sets the version, and the `$blitzecdn_connection_upgrade` map in
  `http.conf.j2` sends `""` unless the visitor is upgrading.
* **Idle timeout.** `blitzecdn_nginx_upstream_keepalive_timeout` (60s) sits
  below an nginx origin's own 75s, so against the most common origin the edge
  closes first. An origin with a shorter timeout (Apache's 5s) closes first
  instead; nginx retries a request whose reused connection turns out closed,
  except a non-idempotent one it had already sent.

## DNS re-resolution

With `blitzecdn_nginx_resolvers` set, each group's server carries `resolve`,
so the origin's address follows DNS at the resolver's `valid` interval rather
than being pinned at load, and an origin whose name does not resolve yet fails
its own requests rather than the reload.

`resolve` needs the group in shared memory. Every group on the edge shares one
zone (`blitzecdn_nginx_upstream_zone`), so its size is one fleet-wide setting.
Measured against the edge image, a megabyte holds about seven hundred groups
and a site needs at most one per listener port, so 32m covers well over a
thousand fully proxied sites. Too small fails `nginx -t` with "no memory in
upstream zone" before activation, never at runtime.

## The origin hop

nginx's cache reads an entry's lifetime and exclusions from the response with
directives that take no variables (`proxy_cache_valid`,
`proxy_ignore_headers`). The only per-site, per-response lever is the response
itself.

A capability that needs that lever contributes an `origin` fragment. When any
renders for an origin, the site's listeners for it proxy to a loopback server
of their own on `blitzecdn_nginx_origin_hop_port`, told apart by `Host`
(`<site>.<scheme>-<port>.hop.blitzecdn.invalid`), with the real origin `Host`
in `BZ-Origin-Host`. That hop server's location carries the fragments, so they
rewrite the origin's headers before the cache, one hop up, reads them. A site
with no such fragment is proxied straight to its origin and pays nothing.

The hop server does what `proxy()` does, less what the visitor-facing server
already did:

* `X-Forwarded-*` and `BZ-*` headers arrive set and pass through; the body was
  already limited and read; the visit was already logged and counted.
* Neither direction is buffered: the cache-side server buffers both, and a
  second on-disk copy of a large object buys nothing.
* `X-Accel-*` controls are the origin's instructions to the edge, so the hop
  passes them up for the cache-side server to act on rather than obeying them.
  `X-Accel-Expires` is the origin fragments' to pass or replace.
* `limit_conn` is edge-wide, and every hop request comes from 127.0.0.1;
  counted against one address the hop would cap the whole edge's misses at one
  visitor's limit. `limit_conn_dry_run` keeps the count and drops the cap;
  visitors stay limited where they connect.
