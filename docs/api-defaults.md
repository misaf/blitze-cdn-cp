# API request defaults

These rules describe `/v3`.

## Paths

Each area has one prefix, and a resource one name:

| Area | Paths |
| --- | --- |
| Zones, records, rules | `/v3/domains/...` |
| Sites (what zones and rules resolve to; read-only) | `/v3/sites`, `/v3/sites/{site}` |
| A site's certificate | `/v3/sites/{site}/certificate/...` |
| The fleet's certificates and SSL modes | `/v3/certificates/...`, including `/v3/certificates/ssl-modes/reconcile` |
| Traffic and analytics | `/v3/traffic/...`, a site's under `/v3/traffic/sites/{site}` |
| Deployments, drift checks, rollbacks | `/v3/deployments/...` |

A drift check is a deployment in check mode, `POST /v3/deployments` with
`"check": true`; its report is `GET /v3/deployments/{id}/drift`. A rollback,
`POST /v3/deployments/rollback`, is a deployment too.

`GET /v3/releases/current/diff` answers "what would change on the edges if I
deployed now": a unified diff, per rendered file, between one baseline and the
release current state compiles to. It records nothing. The baseline,
`baseline_release`, is the release of the newest real run that reached any
edge, so after a canary, or a rollout that failed once earlier edges had
committed, it is not what every edge runs. `fleet` says what deployment history
records for each registered edge: its release, the run that verified it there,
and a `state` of `recorded`, `unverified` (a newer run failed or stopped on it
after activating) or `unknown` (no verified run on record, or no deployment
yet). `fleet.releases` groups the edges by release, marks the baseline, and
says per release whether deploying would change it (`changed`, `null` for edges
with nothing verified). `fleet.mixed` is true when the edges sit in more than
one group, and `fleet.warnings` says in words every way the baseline falls
short, an empty fleet included. `fleet.source` is always `deployment_records`:
this is what deployments recorded, not a probe of what an edge serves now; run
a drift check for that. For one group's files, write its tree with
`blitzecdn release render --release <id>` and compare it with the current one. Each deployment
records the release the fleet ran when it was queued, and
`GET /v3/deployments/{id}/diff` shows what that deployment changed. Secrets are
never in a rendered bundle, so never in a diff.

A request that only writes controller state answers synchronously: resource
creation, `PATCH`, `DELETE` of controller records, and installing an uploaded
certificate with `PUT /v3/sites/{site}/certificate`. A request that contacts
ACME, an origin or an edge is queued and answers `202`; see
[queued operations](queued-operations.md). The API models publish defaults in `/openapi.json`
and `/docs`; the owning policy and domain models define their meaning. Tests
compare the HTTP creation defaults with those contracts. Changing a default
can change client behavior even when the field stays optional.

## Creation and updates

| Input | Meaning |
| --- | --- |
| Omitted field on creation | Use the model's creation default, if it has one |
| Missing required field | Reject the request with `422` |
| Omitted field in PATCH | Preserve its stored value |
| Explicit `null` | Clear a nullable value; it does not restore a default |
| `null` for a non-nullable stored setting | Reject the update with `422` |
| Unknown field | Reject the request with `422` |

PATCH schemas distinguish an optional field from a nullable value. Non-nullable
fields may be omitted, but reject explicit `null` during request validation.
For example, `origin_sni: null` clears the override, whereas
`cache_enabled: null` is invalid. Send `cache_enabled: true` to restore that
setting's creation default. A rejected update leaves stored state intact.

## Concurrent edits

Two writers changing different fields of one resource at the same moment both
keep their change: the service reads the resource inside its transaction and
merges only the fields that were sent. What that cannot catch is an edit built
from an earlier read, such as a `firewall` list sent whole with one address
added, or a record `value` worked out from the old one. If another writer
changed the resource in between, the second write silently drops the first.

Single resources carry an `ETag` so a client can say which version its edit
was based on:

| Resource | `ETag` on | `If-Match` on |
| --- | --- | --- |
| Zone `/v3/domains/{domain}` | `GET`, `POST`, `PATCH` | `PATCH`, `DELETE` |
| Record `/v3/domains/{domain}/records/{type}/{record}` | `GET`, `POST`, `PATCH` | `PATCH`, `DELETE` |
| Rule `/v3/domains/{domain}/rules/{rule}` | `GET`, `POST`, `PATCH` | `PATCH`, `DELETE` |
| Edge `/v3/edges/{edge}` | `GET`, `POST`, `PATCH` | `PATCH`, `DELETE` |

Send the `ETag` of the read back as `If-Match`. If the resource has changed
since, the request answers `412` with `{"detail": ...}` and writes nothing;
read it again and reapply the edit. The tag is strong and changes exactly when
the stored resource does, so a `PATCH` that changes nothing keeps it.
`If-Match: *` matches any current version, a list of tags matches any of them,
and a weak tag (`W/"..."`) never matches. The check is made against the version
the write is made against, inside the same transaction. For edge removal it is
made when the removal is queued.

`If-Match` is optional. Without it a write behaves as it always has: the last
write wins, field by field. A missing resource answers `404`, not `412`, and a
request that fails validation answers `422` whether or not its tag matches.

The CLI sends no `If-Match`. Each command sends the values given on its
command line rather than an edit built from an earlier read, and the service
merges them into the version it reads inside its transaction, so there is no
stale read to guard.

## Validation errors

Request validation and validation after merging a PATCH both return HTTP 422
with the same JSON structure:

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "cache_enabled"],
      "msg": "Value error, null is not allowed; omit the field to keep its value"
    }
  ]
}
```

`detail` is always a list. Each issue contains only `type`, `loc`, and `msg`;
raw input, exception context, and Pydantic documentation URLs are omitted.
`loc` starts with the request location, such as `body` or `query`, followed by
field names or array indexes. Cross-field errors can identify a whole model
rather than a single field. For malformed JSON, the body location includes the
parser position. Clients should use `type` and `loc` rather than parsing `msg`.

OpenAPI publishes this response as `ValidationErrorResponse`.

## Other errors

Every other error answers `{"detail": "message"}`, published as
`ErrorResponse`. Each operation declares the statuses it can return:

| Status | When | Declared on |
| --- | --- | --- |
| `400` | Malformed `Content-Length` | Operations with a request body |
| `401` | `X-API-Key` missing or unknown | Every operation behind an API key |
| `403` | Client IP not in `allowed_ips` | Every operation |
| `404` | A named resource does not exist | Operations with a path parameter; rollback, cache purge and certificate renewal |
| `409` | The request conflicts with current state, or reuses an `Idempotency-Key` | Every write, and the drift report of a deployment that was not a check |
| `409` + `Retry-After` | Another deployment holds the deployment lock | `POST /v3/deployments`, `POST /v3/deployments/rollback`, `PUT /v3/sites/{site}/certificate` |
| `412` | The resource changed since the version named in `If-Match` | `PATCH` and `DELETE` of zones, records, rules and edges |
| `413` | Body over `api_max_body_bytes` | Operations with a request body |
| `500` | The installation is wrong; retrying will not help | Every operation |
| `503` | No API keys are configured | Every operation behind an API key |

The three with `Retry-After` take the deployment lock while the request is
open, to queue their work or install the certificate. Only their `409` with `Retry-After`
clears on its own; retry any other `409` only after changing the request or the
state it conflicts with. No operation drives edges while the request is open:
that work is queued, answers `202`, and reports an edge failure on the
operation rather than as `502`.

`GET /health` answers `503` with `HealthUnavailable`, not `ErrorResponse`:
`{"status": "unavailable", "check": "<name>", "detail": "<exception type>"}`.

## Domain policy

Creating a domain requires `name`. Omitted policy fields use these values:

| Fields | Defaults |
| --- | --- |
| `ssl_mode`, `ssl_automatic_mode` | `off`, `auto` |
| `minimum_tls_version` | `1.2` |
| `certificate_mode` | `disabled` |
| `certificate_path`, `certificate_key_path` | `null` |
| `http3_enabled`, `always_use_https`, `under_attack_mode`, `allow_private_origin` | `false` |
| `max_upload_size` | `100m` |
| `enabled`, `cache_enabled` | `true` |
| `cache_level` | `standard`: static file types only, unless the origin sends `Cache-Control`, `Expires` or `X-Accel-Expires`; a request with a cookie skips the cache except for static file URLs |
| `cache_bypass_cookies` | Empty: any cookie under `standard`, none under `everything` |
| `cache_ttl_overrides` | Empty: `cache_valid_success` and `cache_valid_not_found` for every type |
| `cache_ignore_origin_headers` | Empty: the origin's `Cache-Control`, `Expires` and `X-Accel-Expires` are respected |
| `cache_query_string_mode` | `include` |
| `cache_valid_success`, `cache_valid_not_found` | `10m`, `1m` |
| `compression` | `brotli` |
| `error_pages` | Empty: visitors see Nginx's or the origin's own error responses |
| `maintenance_mode`, `maintenance_page`, `maintenance_retry_after` | `false`, `null` (the `503` error page, else a built-in one), `300` |
| `origin_request_host`, `origin_sni` | `null` (no explicit override) |
| Every `firewall` list | Empty |
| `firewall.case_insensitive_paths` | `false` (paths match case-sensitively, as written) |
| `rate_limit` | Empty (no limit); with a `rate`, `burst: 0`, `status: 429`, `paths: []` |
| `visitor_headers.connecting_ip`, `visitor_headers.ip_country` | `true`, `false` |

`ssl_automatic_mode: auto` allows automatic SSL policy selection; it does not
make a newly created `ssl_mode: off` domain immediately serve HTTPS. TLS modes
require active certificate material, and HTTP/3 requires edge TLS.

Records inherit domain policy. A matching rule changes only settings explicitly
present in its `overrides`; omitted overrides inherit the domain value. A rule
requires `name` and a nonempty `overrides` object. Its other defaults are
`priority: 100`, `match: "*"`, and `enabled: true`. The first matching enabled
rule wins, ordered by priority and then name.

### Nested PATCH objects replace whole blocks

`firewall`, `rate_limit` and `visitor_headers` are replacement objects, not
recursive merges.
For example:

```json
{"firewall": {"deny_sources": ["192.0.2.0/24"]}}
```

This replaces every firewall list: lists not supplied become empty, and
`case_insensitive_paths` returns to `false`. Omit the
entire `firewall` field to preserve existing rules; send `{}` to clear them.
`rate_limit: {}` removes the limit. Likewise, `visitor_headers: {}` restores both header switches to their defaults.
Read the current block and include all values you want to retain when replacing
it, and send the read's `ETag` as `If-Match` so a change made in between is
refused rather than overwritten (see [concurrent edits](#concurrent-edits)). A
rule PATCH also replaces its entire `overrides` object when supplied.

### Rate limits

`rate_limit` caps the requests each client address makes to a site:

```json
{"rate_limit": {"rate": "10r/m", "burst": 5, "paths": ["/login"]}}
```

`rate` is one of `10r/m`, `30r/m`, `1r/s`, `2r/s`, `5r/s`, `10r/s`, `20r/s`,
`50r/s`, `100r/s`, `200r/s`, `500r/s` or `1000r/s`. Nginx fixes a rate per
shared-memory zone, so every site on the same rate shares one zone, keyed per
site and client; the ladder keeps an edge's rate-limit memory bounded (5 MiB
per rate in use, 60 MiB at most) however many sites enable it. `burst` requests
above the rate are served at once rather than queued; beyond that a client gets
`status` (`429` or `503`). `paths` narrows the limit to the same exact or
`/`-terminated prefix paths as `firewall.allowed_paths`, and like them match
regardless of case when `firewall.case_insensitive_paths` is on. Addresses in
`firewall.allow_sources` are never limited, and the ACME challenge path is not
rate-limited. Refused requests appear in the access log and traffic accounting.

## Records and edges

| Resource | Required fields | Defaults |
| --- | --- | --- |
| DNS record | `name`, `value` | `type: A`, `ttl: 300`, `proxied: true` |
| Edge | `name`, `host` | `user: deploy`, `port: 22`, `private_key_file: null`, `public_addresses: []`, `ssh_sources: []`, `capabilities: null` |

Create records with `POST /v3/domains/{domain}/records`. The URL supplies the
domain; including `domain` in the body is rejected with `422`. Responses retain
`domain`, and the zone must already exist. A minimal creation body is:

```json
{"name": "www", "value": "192.0.2.10"}
```

`proxied: true` treats `value` as the origin; `false` publishes it as the DNS
answer. A proxied record's `type` picks only which edge address DNS publishes,
so its origin may be IPv4 or IPv6; an unproxied record's value must match its
type. A hostname served over both IPv4 and IPv6 is a proxied A and a proxied
AAAA record with the same `value`, since one virtual host has one origin.
Record PATCH fields do not accept explicit `null`.

One record is addressed by type and name,
`/v3/domains/{domain}/records/{type}/{record}`, for `GET`, `PATCH` and `DELETE`.
An A and an AAAA record can share a name, so the type is part of the path and
never assumed; an unknown type is a `422`.

An edge with no `public_addresses` falls back to its management `host`.
`capabilities: null` uses the controller's installed capability set; an explicit
empty list declares none. Edge PATCH lists replace wholesale; empty lists clear
them. Omitted PATCH fields preserve the existing edge values.

## Operational requests

| Field | Default and meaning |
| --- | --- |
| `host_limit` | Omitted or `null`: all registered edges |
| Deployment/rollback `check` | `false`: apply changes; `true`: Ansible check mode |
| Rollback `deployment_id` | Omitted or `null`: select the newest successful full-fleet deployment of a release other than the one the newest real run that reached any edge applied |
| Cache purge `entries`, `prefixes`, `tags`, `purge_all` | `[]`, `[]`, `[]`, `false`; an empty request does not purge everything and is refused |
| Purge entry `scheme` | `https` |
| Purge prefix `prefix` | `/`: every cached response for the hostname, over both schemes |
| Certificate request `email` | Omitted or `null`: use configured `BLITZE_ACME_DEFAULT_EMAIL`; fail if neither supplies an email |
| Certificate request `skip_preflight` | `false` |
| Renewal `within_days`, `force` | `30`, `false` |
| Renewal `sites` | Omitted or `null`: consider all managed certificates; `[]` is invalid |
| Edge removal `decommission`, `force` query parameters | `true`, `false` |

Bodies with all-optional fields still require a JSON object. Send `{}` to use
their defaults. In particular, `POST /v3/deployments` with `{}` requests a real
deployment across the fleet; it is not a dry run. Creation and policy edits
alone do not deploy the resulting configuration.
