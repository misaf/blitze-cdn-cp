# API request defaults

These rules describe `/v3`. The API models publish defaults in `/openapi.json`
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

OpenAPI publishes this response as `ValidationErrorResponse`. Authentication,
missing-resource, conflict, and execution errors retain their own status codes
and `{"detail": "message"}` format. Invalid server responses remain server errors.

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
it. A rule PATCH also replaces its entire `overrides` object when supplied.

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
answer. Record PATCH fields do
not accept explicit `null`. Record update/delete queries default `type` to `A`;
specify `AAAA` when addressing an IPv6 record.

An edge with no `public_addresses` falls back to its management `host`.
`capabilities: null` uses the controller's installed capability set; an explicit
empty list declares none. Edge PATCH lists replace wholesale; empty lists clear
them. Omitted PATCH fields preserve the existing edge values.

## Operational requests

| Field | Default and meaning |
| --- | --- |
| `host_limit` | Omitted or `null`: all registered edges |
| Deployment/rollback `check` | `false`: apply changes; `true`: Ansible check mode |
| Rollback `deployment_id` | Omitted or `null`: select the newest successful applied deployment of a different release |
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
