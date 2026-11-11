# Path rules

Code: `path_locations` and `fragments` in
`assets/ansible/roles/blitzecdn_nginx/templates/site.conf.j2`, and every
capability fragment that reads `site.variable_scope`.

## Location order

A host's path rules render as regex locations, in the order they are matched.
nginx takes the first matching regex location in the order written, which is
what makes "first path rule wins" hold however the paths nest. A prefix
location would instead be chosen by length.

Each location's regex is its `pattern`, which the controller builds with
`path_pattern` and the template renders as given, so the rule is spelled once.

Rules match the normalized `$uri`, as the firewall's paths do. Nothing a path
rule declares can shadow the edge's own paths:

* The `^~` prefixes (the ACME challenge, `/.blitzecdn/`, and the firewall's
  case-sensitive denied paths) stop the regex search.
* The firewall's case-insensitive denied paths are written before the path
  rules.

An HTTP listener that redirects to HTTPS renders no path rules: `location /`
sends every request to HTTPS, where the rules apply.

## Scopes

A fragment sees `site` as either the host or, inside a path rule's location,
the host with that rule's settings merged in. The merged scope carries
`variable_scope`, which names the http-level variables (maps) a path-scoped
setting is read through, so a path never reads its host's. Capabilities that
declare per-site maps declare one per path rule beside the host's.

## Redirect suffixes

A redirect target containing `$1` takes the suffix from `$request_uri`, still
percent-encoded as the visitor sent it. A capture of the decoded `$uri` could
carry a CR or LF into the `Location` header. A request whose raw form spells
the path differently (`/%6Fld/`) still matches the location and is redirected
without its suffix.

## Redirects and the firewall

A redirect is answered only after the host's own fragments have run, as a
request to the host's `location /` would be: maintenance mode, the firewall's
country, method, path and source rules, the rate limit, and Under Attack Mode.
A client the firewall refuses gets its 403 or 429, not the redirect.

The `return` sits two named locations behind the regex one. `return` runs in
nginx's rewrite phase, so written beside the fragments it would answer before
`limit_req` (preaccess), `allow`/`deny` and Under Attack Mode's `auth_request`
(access) were consulted. The first `try_files` hop leaves the access
fragments' phases behind; the second does the same for the upstream
fragments, which is where Under Attack Mode's `auth_request` lives.

A redirect rule carries no settings of its own, so its fragments read the
host's scope.
