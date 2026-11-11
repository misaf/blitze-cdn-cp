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
  purge therefore matches the exact key on each cached file's `KEY` line in the
  scanner's walk, like prefixes and tags, instead of deleting the one hashed
  path.
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
