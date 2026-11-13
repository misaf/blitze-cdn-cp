# Cache

Code: `capabilities/cache/nginx/cache-http.conf.j2` (http-level maps),
`capabilities/cache/nginx/cache-upstream.conf.j2` (per-location directives),
`capabilities/cache/nginx/cache-origin.conf.j2` (origin hop).

## What is never cached

`proxy_cache_bypass` (do not serve from cache) and `proxy_no_cache` (do not
store the response) name the same request conditions. Either alone leaks:
bypass alone still stores a private answer for the next anonymous visitor, and
no_cache alone still serves that visitor an entry stored earlier.

* `$http_upgrade` keeps WebSocket handshakes out.
* `$http_authorization`: nginx honours `Set-Cookie` and
  `Cache-Control: private|no-cache|no-store` on the response, but ignores the
  request's credentials. An origin that authorizes a request and answers
  without `Cache-Control` would otherwise have that answer cached under a key
  no credential is part of, and served to everyone.
* The site's cookie map, `$blitzecdn_cache_cookie_<scope>`: a cookie is a
  credential too, and the common case (a framework sets a session cookie at
  login and renders `/account` without `Cache-Control`).
* Under `standard` only, `$blitzecdn_cache_standard_skip` on `proxy_no_cache`.
  It reads the response, so it cannot be a bypass condition.

The cookie map is declared in `cache-http.conf.j2` under exactly the condition
`cache-upstream.conf.j2` reads it (cache enabled, and level `standard` or bypass
cookies listed). The two must agree, or a site names an undeclared variable
and the edge refuses its configuration.

## Cache levels

* **`standard`.** `$blitzecdn_cache_static_uri` is 1 for a static file
  extension, judged before the origin answers. A static URL is cached even
  when the visitor carries a session cookie, since browsers send cookies with
  every image and stylesheet. Any other response is stored only if its
  `Content-Type` is a static type or the origin gave it a lifetime
  (`Cache-Control`, `Expires` or `X-Accel-Expires`). Otherwise nginx would fall
  back to `proxy_cache_valid` and store an HTML page for its full TTL, which is
  the leak `standard` exists to close. With no bypass cookies listed, any
  cookie bypasses a non-static URL.
* **`everything`.** Only listed cookies bypass, on every URL.

Maps that read the origin's response headers are `volatile`: only directives
evaluated once the response is in (`proxy_no_cache`, `add_header`) may read
them, and a value cached from an earlier evaluation would predate the
response.

## Cache key

`$scheme$server_port$request_method$host<uri>$blitzecdn_accept_encoding`.

* **The listener port is load-bearing.** The edge keeps the visitor's
  destination port toward the origin (see
  [origin connections](origin-connections.md)), and an origin may run two
  services on two ports. Without `$server_port` whichever port filled an entry
  first would answer for every other.
* **Not the origin port.** It equals the listener port except Flexible on 443,
  which reaches origin:80. That request already differs from a plain `:80` one
  by `$scheme`, and must: the origin sees `X-Forwarded-Proto` and may answer
  the two differently.
* **Cost.** One URL can be cached once per listener, so a named purge deletes
  every port's variant; `blitzecdn_cache_purge` walks the same listener sets.
* **Vary.** nginx honours the origin's `Vary`: the first representation is
  filed at the MD5 of the key, every other one under a hash of the key and the
  varied request header values. `Vary: Accept-Encoding` splits even one
  normalized encoding, since nginx hashes the visitor's raw header. A named
  purge therefore matches the exact key on each cached file's `KEY` line,
  through the purge index like prefixes and tags, instead of deleting the one
  hashed path.
* **Encoding.** The key names the encoding, so the origin is asked for exactly
  that one (`Accept-Encoding: $blitzecdn_accept_encoding`). When the
  compression capability is installed and active it sends the same header, and
  the cache fragment does not repeat it.
* **Query strings.** `cache_query_string_mode: ignore` keys on
  `$blitzecdn_uri_without_query` instead of `$request_uri`.

## TTL overrides and ignored origin headers

Per-site maps, read in the origin hop by `cache-origin.conf.j2`, which sets
`X-Accel-Expires` for the cache one hop up.

* An entry names a URL extension (`.css`), a content type (`text/html`) or a
  family (`image/*`), matched against `"<Content-Type>|<path>"`. Extensions
  render first, then exact types, then families; nginx takes the first
  matching regex, so the most specific entry wins.
* An override is the TTL for responses the origin gave no lifetime of its own.
  It applies to the statuses `cache_valid_success` covers (200, 301, 302) and
  to 304, so a revalidation renews the entry for as long as a fresh fetch.
* An ignored type is stored for its override if any, else for the site's
  `cache_valid_success` (or `cache_valid_not_found` on 404); any other status
  of it is not stored, since the origin's word on that is what is ignored.
* Everything else passes the origin's own `X-Accel-Expires` through.

## Revalidation

A conditional revalidation renews an entry from the 304's headers, and a 304
need not carry `Content-Type`. The hop's maps see only the 304, so an entry
kept for a type's override, or stored despite the origin's headers because of
its type, would fall back to the origin's lifetime on first renewal. A site
with any entry naming a type therefore turns `proxy_cache_revalidate` off and
refetches in full. Extension entries match the URL, which every response has.

## Purge tags

The origin names an entry's purge tags in `Cache-Tag`. The header is hidden
from visitors, but nginx still stores it with the entry, which is where
`blitzecdn_cache_purge`'s scanner reads it back to purge by tag.

## Purge index

Nothing in a cache file's name says which URL, prefix or tag it belongs to:
the name is a hash, and a `Vary` variant's is a hash of more than the key. So
`blitzecdn_cache_purge` keeps an index on each edge
(`/var/lib/blitzecdn/cache-index.sqlite3`) of what each cached file holds: its
`KEY` line, the key from the hostname on for prefixes, and the tags in its
stored `Cache-Tag` header. A named, prefix or tag purge is a lookup in it.

* **Maintenance.** Every purge brings the index up to date before using it,
  and reads only what changed. nginx stores an entry by renaming a finished
  file into place and evicts one by unlinking it, and both change the mtime
  of the directory holding it. A directory whose mtime matches the index is
  not listed; in one that changed, only files the index has not seen (by
  inode, size and mtime) are read. A purge on an unchanged cache costs a
  `stat` per cache directory and reads no files.
* **Same-tick changes.** A directory whose mtime is less than two seconds old
  when listed is recorded as unsettled and listed again next time, because the
  kernel's timestamps are coarser than the changes they record.
* **Derived data.** A missing, corrupt or foreign index (another cache path,
  device, tag header or schema version) is discarded and rebuilt by reading
  every file once. That is what every purge cost before the index.
* **Outside the cache.** nginx's cache loader deletes files it does not
  recognise under `proxy_cache_path`, so the index lives beside the cache, not
  in it. `blitzecdn_cache_teardown` removes it on decommission.
* **Reporting.** Every purge kind reports `scanned`, `read`, `removed` and
  `unchecked` per edge, emptying the cache included. A file the index cannot
  decide, or a matched file that could not be deleted, fails the edge.
* **Concurrency.** The index's write transaction serializes purges on an
  edge. It does not stop nginx storing, replacing or evicting entries while a
  purge runs; see below.
* **Delivery.** The purge is still an SSH play.

## Concurrent cache activity

A purge does not pause nginx and does not see the cache as a snapshot. nginx
keeps storing, replacing and evicting entries while a purge runs. A purge
takes its view of the cache when it brings its index up to date, then deletes
what matched. Its guarantee is stated from that point, called *the scan*
below. A purge queued behind another purge on the same edge scans only once
the earlier purge has finished.

* **Guarantee.** When an edge reports `unchecked: 0`, every cache file that
  matched the purge and was already in place when the scan began is gone. That
  covers every `Vary` representation, named, prefix and tag purges alike, and
  holds whether the index was cold, warm or rebuilt. nginx then fetches those
  URLs from the origin again.
* **Fills that land later.** nginx writes an entry to a temporary file and
  renames it into place once the origin's response is complete, which can be
  a moment after the visitor has received all of it. A purge ignores
  temporary files, because nothing is served from them. So an entry
  renamed into place after the scan began can survive the purge, even if its
  request reached the origin before the purge started and it holds content
  from before the change. A `Vary` representation that survives this way is
  served again once nginx stores the URL's primary file. **Purge again** once
  the origin serves the new content. A repeat purge removes whatever landed
  since the previous one.
* **Replacements.** nginx replaces an entry by renaming a new file over the
  same path. If that happens between the scan and the deletion, the purge
  deletes the new file too. A purge prefers deleting too much to serving stale
  content. A replacement that lands after the deletion is a later fill, as
  above.
* **Eviction.** A matched file that nginx evicted before the purge reached it
  counts as neither `removed` nor `unchecked`. `removed` counts only the files
  this purge deleted. nginx keeps a purged entry's node in shared memory until
  the entry would expire. When its cache manager then fails to unlink the
  file, it logs an `unlink() ... failed (2: No such file or directory)` line
  at `crit`. That line is expected and harmless.
* **Incomplete purges.** A file the purge could not read, could not decide
  (no `KEY` line, or a key or header longer than the scanner reads) or could
  not delete is left in place. It is still served, and it counts as
  `unchecked`. The role fails that edge, and the purge result marks it as not
  complete. Every repeat of the purge reports such a file again until it can
  be read and deleted, or the cache is emptied.

`tests/capabilities/cache/test_cache_purge_nginx.py` holds the scanner to
these rules against a real nginx. The tests trigger concurrent fills,
replacements and evictions at a fixed point inside the purge, and also run
purges against full-speed load with eviction. Once the edge agent proposed
  in issue 269 exists, it can carry the same request and keep the index warm
  between purges.
