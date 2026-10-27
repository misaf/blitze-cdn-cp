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

## Domain policy

Creating a domain requires `name`. Omitted policy fields use these values:

| Fields | Defaults |
| --- | --- |
| `ssl_mode`, `ssl_automatic_mode` | `off`, `auto` |
| `minimum_tls_version` | `1.2` |
| `certificate_mode` | `disabled` |
| `certificate_path`, `certificate_key_path` | `null` |
| `http3_enabled`, `always_use_https`, `under_attack_mode` | `false` |
| `max_upload_size` | `100m` |
| `enabled`, `cache_enabled` | `true` |
| `cache_query_string_mode` | `include` |
| `cache_valid_success`, `cache_valid_not_found` | `10m`, `1m` |
| `compression` | `brotli` |
| `origin_request_host`, `origin_sni` | `null` (no explicit override) |
| Every `firewall` list | Empty |
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

`firewall` and `visitor_headers` are replacement objects, not recursive merges.
For example:

```json
{"firewall": {"deny_sources": ["192.0.2.0/24"]}}
```

This replaces every firewall list: lists not supplied become empty. Omit the
entire `firewall` field to preserve existing rules; send `{}` to clear them.
Likewise, `visitor_headers: {}` restores both header switches to their defaults.
Read the current block and include all values you want to retain when replacing
it. A rule PATCH also replaces its entire `overrides` object when supplied.

## Records and edges

| Resource | Required fields | Defaults |
| --- | --- | --- |
| DNS record | `domain`, `name`, `value` | `type: A`, `ttl: 300`, `proxied: true` |
| Edge | `name`, `host` | `user: deploy`, `port: 22`, `private_key_file: null`, `public_addresses: []`, `ssh_sources: []`, `capabilities: null` |

The record body's `domain` must match the URL. `proxied: true` treats `value`
as the origin; `false` publishes it as the DNS answer. Record PATCH fields do
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
| Cache purge `entries`, `purge_all` | `[]`, `false`; an empty request does not purge everything and is refused |
| Purge entry `scheme` | `https` |
| Certificate request `email` | Omitted or `null`: use configured `BLITZE_ACME_DEFAULT_EMAIL`; fail if neither supplies an email |
| Certificate request `skip_preflight` | `false` |
| Renewal `within_days`, `force` | `30`, `false` |
| Renewal `sites` | Omitted or `null`: consider all managed certificates; `[]` is invalid |
| Edge removal `decommission`, `force` query parameters | `true`, `false` |

Bodies with all-optional fields still require a JSON object. Send `{}` to use
their defaults. In particular, `POST /v3/deployments` with `{}` requests a real
deployment across the fleet; it is not a dry run. Creation and policy edits
alone do not deploy the resulting configuration.
