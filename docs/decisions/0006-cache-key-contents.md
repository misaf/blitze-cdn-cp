# 0006: Put the listener port and the encoding in the cache key

Status: Accepted

## Context

The edge keeps the visitor's destination port toward the origin, and an
origin may run different services on different ports. The edge also asks the
origin for one normalized content encoding rather than passing the visitor's
`Accept-Encoding` through. A key that left either out would let one port's or
one encoding's response answer for another.

## Decision

The cache key is
`$scheme$server_port$request_method$host<uri>$blitzecdn_accept_encoding`:

- The listener port (`$server_port`), not the origin port. They differ only
  for Flexible SSL on 443, which reaches origin port 80, and that request
  already differs from a plain `:80` request by `$scheme`.
- The normalized encoding. The origin is asked for exactly that encoding.
- `cache_query_string_mode: ignore` keys on the URI without its query string.

The [cache](../design/cache.md#cache-key) design note has the full reasoning,
including how nginx files `Vary` variants.

## Consequences

- One URL can be cached once per listener and per encoding.
- A purge of a named entry must remove every port's and every variant's copy.
  It matches the exact key on each cached file's `KEY` line through the
  edge-side purge index, rather than deleting one hashed path.
- Changing the key's contents invalidates every cached object on the next
  deployment. Treat it as a deliberate, announced change.
