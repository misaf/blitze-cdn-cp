# Queued API work

Queued endpoints return `202 Accepted` once work has been recorded. This means
the request was accepted; it does not mean the remote operation succeeded.

Every accepted response includes:

| Header | Meaning |
| --- | --- |
| `Location` | Relative URL of the status resource to read with `GET` |
| `Retry-After: 2` | Suggested delay in seconds before the next status read |
| `Cache-Control: no-store` | Read current status rather than a cached response |

The JSON body is the resource's initial representation. Its `id` remains the
same on subsequent reads. Follow `Location` rather than guessing the polling
path from the submission URL, especially for drift checks and rollbacks.

## Submit and poll

```http
POST /v3/cache/stats
X-API-Key: YOUR_API_KEY
Content-Type: application/json

{}
```

```http
HTTP/1.1 202 Accepted
Location: /v3/operations/OPERATION_ID
Retry-After: 2
Cache-Control: no-store
```

Wait for the suggested interval, then send:

```http
GET /v3/operations/OPERATION_ID
X-API-Key: YOUR_API_KEY
```

A successful status read returns `200`, including when the recorded work has
failed. Inspect the body's `status` and `error`. While work is pending or running,
the response includes `Retry-After: 2`. Finished resources omit that header.
All successful status reads include `Cache-Control: no-store`.

Use the same API hostname and credentials when following the relative
`Location`. The polling interval is a hint, not an estimated completion time.
Set a client timeout for the overall wait; stopping polling does not cancel
server work. Polling does not resubmit the original request.

## Resources and outcomes

| Submission | Polling resource | Response body |
| --- | --- | --- |
| `POST /v3/cache/purge` | `/v3/operations/{id}` | `Operation`; result is `PurgeResult` |
| `POST /v3/cache/stats` | `/v3/operations/{id}` | `Operation`; result is `CacheStatsReport` |
| `POST /v3/origins/check` | `/v3/operations/{id}` | `Operation`; result is `OriginReport` |
| `DELETE /v3/edges/{name}` | `/v3/operations/{id}` | `Operation`; result describes removal and decommissioning |
| `POST /v3/deployments` | `/v3/deployments/{id}` | `Deployment` |
| `POST /v3/drift` | `/v3/deployments/{id}` | Check-mode `Deployment` |
| `POST /v3/rollbacks` | `/v3/deployments/{id}` | Rollback `Deployment` |

Operations use `pending`, `running`, `succeeded`, and `failed`. On success,
inspect the operation-specific `result`: a completed purge can still report
`complete: false` and list `failed_hosts`. Origin reports describe reachability
per origin. A job finishing successfully does not guarantee every edge probe
or action succeeded.

Deployments use `queued`, `running`, `succeeded`, `failed`, `timed_out`, and
`abandoned`. The last four are terminal. Check `status`, `error`, and the
deployment result; per-edge progress is at
`GET /v3/deployments/{id}/targets`. After a completed drift check, read
`GET /v3/deployments/{id}/drift` for the report.

Only successful admission returns the polling headers. Validation, authentication,
and admission errors keep their normal HTTP error responses. A `409` caused by
a busy deployment may separately include `Retry-After: 30`; that is an admission
retry hint, not a link to newly accepted work. Do not automatically repeat a
mutating request after a network timeout: it may already have been accepted.

Certificate endpoints that return a reconciliation report with an optional
`deployment` remain synchronous at the outer request level. If that report
contains a queued deployment, poll `/v3/deployments/{deployment.id}` using the
same deployment status rules.
