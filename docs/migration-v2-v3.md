# Migrating clients from API v2 to v3

Collection GET responses use an `{items, total, limit, offset}` envelope.
Read collection data from `items` and follow the [pagination contract](api-pagination.md).

This guide compares the deployed BlitzeCDN **2.6.1 `/v2` contract**, inspected
on 2026-10-27, with the **current `master` `/v3` contract**. It is not a promise
that every historical API v2 implementation has the same behavior.

The published `6.1.0` image still exposes the newer implementation as `/v1`.
The `/v3` prefix, simplified record creation, PATCH schema corrections, and
polling headers described here are subsequent local changes. Deploy a release
containing those changes before migrating clients; routing alone cannot add
them to the published image.

API versions are independent of application releases. OpenAPI's `info.version`
reports the application version; it does not determine the URL prefix.

## Connection and documentation

Keep using `X-API-Key`. Obtain credentials and allowlist access for the target
environment; separate instances do not automatically share keys or data.
Generate clients from that instance's `/openapi.json`, and use its `/docs`.
`/health` and `/metrics` remain unversioned.

Change the API base prefix from `/v2` to `/v3`, then apply the changes below.
There are no `/v1` or `/v2` compatibility routes in the current application.
An old domain or deployment ID is not automatically present on another instance.

## Route changes

| API v2 | API v3 |
| --- | --- |
| `GET /v2/sites` | `GET /v3/hosts` |
| `GET /v2/sites/{name}` | `GET /v3/hosts/{name}` |
| `GET /v2/sites/{name}/certificate` | `GET /v3/hosts/{name}/certificate` |
| `GET /v2/sites/{name}/certificate/preflight` | `GET /v3/hosts/{name}/certificate/preflight` |
| `POST /v2/sites/{name}/certificate/upload` | `POST /v3/hosts/{name}/certificate/upload` |
| `POST /v2/sites/{name}/certificate/request` | `POST /v3/hosts/{name}/certificate/request` |

Other existing route suffixes remain available under `/v3`, but their payloads
and response behavior are not necessarily unchanged. New resources include
domain policy, hostname rules and resolution, queued operations, retained
releases, deployment targets, and controller status.

## Records and serving policy

In v2, DNS records carry serving policy such as SSL, caching, compression, and
firewall settings. In v3:

- The domain owns the default serving policy.
- Records contain only DNS/origin fields.
- Rules override domain policy for matching hostnames.
- `/hosts` returns the derived serving hosts; it is read-only.

For example, this v2 creation body disables caching and selects gzip for one
proxied hostname:

```http
POST /v2/domains/example.com/records
Content-Type: application/json
X-API-Key: YOUR_API_KEY

{"domain":"example.com","name":"www","value":"192.0.2.10","proxied":true,"cache_enabled":false,"compression":"gzip"}
```

On v3, ensure the domain exists, then create the record without `domain` or
policy fields in the body:

```http
POST /v3/domains/example.com/records
Content-Type: application/json
X-API-Key: YOUR_API_KEY

{"name":"www","value":"192.0.2.10","proxied":true}
```

Add a hostname-specific rule to preserve that record's policy:

```http
POST /v3/domains/example.com/rules
Content-Type: application/json
X-API-Key: YOUR_API_KEY

{"name":"www-policy","match":"www.example.com","overrides":{"cache_enabled":false,"compression":"gzip"}}
```

If a setting should apply to every hostname instead, update it through
`PATCH /v3/domains/{domain}`. Do not copy one record's policy onto the whole
domain without comparing the other hostnames. Rule priority defaults to `100`;
the first matching enabled rule wins, ordered by priority and then name.
Rules do not accumulate overrides from later matches.

Inspect the effective policy before deployment:

```http
GET /v3/domains/example.com/resolve?hostname=www.example.com
X-API-Key: YOUR_API_KEY
```

Record creation requires only `name` and `value`. It defaults to `type: A`,
`ttl: 300`, and **`proxied: true`**, whereas v2 defaults `proxied` to `false`.
Send an explicit proxy flag during migration. A body containing `domain` now
returns `422`, even when it matches the URL. Record responses still include
`domain`, but no longer include serving-policy fields.

Keep `type=AAAA` when addressing an IPv6 record through PATCH or DELETE; the
query default remains `A`. See [request defaults](api-defaults.md) for all
creation values and operation scope.

## PATCH semantics

Omit fields to preserve their stored values. Explicit `null` clears only
nullable settings, such as `origin_sni`; it does not mean "restore the default."
Non-nullable fields reject `null` during request validation with a field-specific
`422` response. Update generated clients to use the corrected schemas.

Nested `firewall` and `visitor_headers` objects replace their whole block.
Omitted nested lists or switches receive creation defaults. A rule PATCH also
replaces its entire `overrides` object when provided. Read and retain all values
you need before replacing a block; see [nested PATCH objects](api-defaults.md#nested-patch-objects-replace-whole-blocks).

## Queued operations

| Request | v2 response | v3 response |
| --- | --- | --- |
| Cache purge | `200 PurgeResult` (or `409` for partial failure) | `202 Operation` |
| Cache statistics | `200 CacheStatsReport` | `202 Operation` |
| Origin checks | `200 OriginReport` | `202 Operation` |
| Edge removal | `200 EdgeRemoval` | `202 Operation` |
| Deploy, drift check, rollback | `202 Deployment` | `202 Deployment`, with polling headers |

For every v3 `202` response, follow `Location` with authenticated `GET`
requests. `Retry-After: 2` suggests the polling interval. Successful status
reads return `200` even when the recorded work failed. They use
`Cache-Control: no-store`, and omit `Retry-After` once the resource is terminal.

Operations finish as `succeeded` or `failed`; read `result` or `error`. A purge
that ran successfully can still report `result.complete: false` and name
`failed_hosts`. Move partial-purge handling from the submission's HTTP status
to the completed operation's result. Do not treat `202` as successful purging
or successful edge removal.

Deployments finish as `succeeded`, `failed`, `timed_out`, or `abandoned`.
The deployment body adds required `release_id` and optional `error` fields.
Poll `/v3/deployments/{id}`, inspect per-edge progress through
`GET /v3/deployments/{deployment_id}/targets`, and read the drift report after
a completed check-mode deployment. See [queued API work](queued-operations.md)
for the full submission and polling contract.

Certificate issuance, renewal, and reconciliation remain synchronous at the
outer request level. Reconciliation may return a nested queued `deployment`;
poll that deployment separately. A workflow journal is not itself an operation
polling resource or a completion guarantee.

## Host identities and certificates

Do not assume old site names are valid v3 host names. V3 groups proxied records
by domain, winning rule, and origin; rule changes or origin splits can change
derived identities. Read `/v3/hosts` and map `server_names` to the returned
`name` before calling host-specific certificate endpoints.

The certificate routes now use `/hosts`, but certificate JSON still uses
`site`, and renewal requests still use `sites`. Do not mechanically rename
those fields. Certificate information adds `certificate_digest` and
`private_key_digest`. Edge representations also add `capabilities`; omitted or
`null` uses the controller's installed set, while `[]` declares none.

Use certificate upload/request endpoints to establish controller-managed
material. They own uploaded/requested certificate modes and immutable paths;
authoring those modes directly in domain or rule policy is refused. Review
effective SSL policy after moving certificates, rather than copying old paths
or assuming an old site name still identifies the same material.

## DNS export consumers

V2 `GET /v2/dns/export` returns a list. V3 `GET /v3/dns/export` returns:

```json
{
  "publication": {
    "mode": "external",
    "publishes": false,
    "detail": "Human-readable publication explanation"
  },
  "records": []
}
```

Update consumers to iterate `records`. Do not parse the human-readable
`publication.detail` or `answer` text as a machine address. Proxied rows omit
the origin `value`; publish the appropriate edge address through your external
DNS system. Unproxied rows retain their DNS answer. Writing records to BlitzeCDN
does not publish them to authoritative DNS.

## Client and server cutover

This guide maps API contracts; it is not an automated database upgrade or a
validated v2-to-v3 state converter. Keep the old and new instances' databases
separate. A gateway serving both prefixes does not synchronize their state.

1. Export and record the old domains, records, per-hostname policy, edge inventory,
   and certificate ownership. Preserve a restorable backup and review local
   infrastructure/template customizations separately from API data.
2. Rehearse on a separate new instance and test edge. Create domain policy,
   records, and rules explicitly, then establish the new certificate identities.
3. Compare hostname resolution, `/hosts`, DNS exports, and certificate status.
   Inspect `/v3/releases/current` and run a check-mode deployment before the
   planned applied rollout. These checks do not replace testing real traffic.
4. Transfer each production edge under a controlled cutover: disable the old
   controller's provisioning, deployment, and renewal automation for that edge
   before the new controller manages it. Never let both controllers converge
   the same edge. Route test clients to test state, not to old production writes.
5. Move clients after validating request bodies, response parsing, polling,
   partial failures, and TLS/DNS behavior. Retire legacy endpoints only after
   their clients have migrated.

Returning a gateway prefix to the old instance does not undo edge configuration
or data changes made by the new one. Plan rollback of state, certificates, DNS,
and edge ownership together; an HTTP routing change alone is insufficient.
