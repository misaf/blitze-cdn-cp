# Collection pagination

Every collection GET uses `fastapi-pagination` and returns the same envelope:

```json
{
  "items": [],
  "total": 125,
  "limit": 100,
  "offset": 200
}
```

`items` contains the selected slice. `total` counts all matching items before
pagination. `limit` defaults to 100 and accepts 1–500; `offset` defaults to zero
and accepts 0–9223372036854775807. Invalid values return 422. An offset at or
beyond the end returns 200 with an empty `items` array and the matching total.
Each item keeps its endpoint's documented representation.

Start with `?limit=100&offset=0`, consume `items`, then increment `offset` by
`limit` until `offset >= total`. Keep any filter parameters on every request.
For example, both requests below select certificates expiring within 30 days:

```text
GET /v3/certificates?expiring_in=30&limit=100&offset=0
GET /v3/certificates?expiring_in=30&limit=100&offset=100
```

## Collections and ordering

| Endpoint | Ordering |
| --- | --- |
| `/v3/domains` | Zone name |
| `/v3/hosts` | Derived hostname order |
| `/v3/domains/{domain}/records` | Record name, then type |
| `/v3/domains/{domain}/rules` | Priority, then rule name |
| `/v3/edges` | Edge name |
| `/v3/deployments` | Creation time descending, then ID descending |
| `/v3/deployments/{deployment_id}/targets` | Edge name |
| `/v3/releases` | Creation time descending, then ID descending |
| `/v3/audit-events` | Audit ID descending |
| `/v3/workflows` | Creation time descending, then ID descending |
| `/v3/certificates` | Certificate expiry, soonest first |

The DNS export endpoint returns a complete export document. Nested collections
inside detail reports are part of that report and are not separately paginated.

## Storage and concurrent changes

Deployment history, deployment targets, releases, audit events and workflows use
the library's SQLAlchemy integration. SQLite counts matching rows and retrieves
only the requested page in the same read transaction. API schemas stay in the
HTTP layer; stores return domain values with their total.

Configuration collections and certificate observations are already materialized
by their readers. The library slices them before converting the selected items
to API representations. This bounds response size, but those readers still load
the complete collection.

Each request sees current data. Inserts, removals, retention pruning and changing
certificate filters can shift offsets between requests. Ordering is deterministic
for an unchanged collection; pagination does not create a snapshot spanning
requests. CLI list commands retain their own output and limits.
