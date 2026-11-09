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
POST /v3/cache/connections
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
| `POST /v3/cache/connections` | `/v3/operations/{id}` | `Operation`; result is `ConnectionsReport` |
| `POST /v3/origins/check` | `/v3/operations/{id}` | `Operation`; result is `OriginReport` |
| `DELETE /v3/edges/{name}` | `/v3/operations/{id}` | `Operation`; result describes removal and decommissioning |
| `POST /v3/hosts/{name}/certificate/request` | `/v3/operations/{id}` | `Operation`; result is `CertificateInfo` |
| `POST /v3/certificates/renew` | `/v3/operations/{id}` | `Operation`; result is a renewal report |
| `POST /v3/certificates/reconcile` | `/v3/operations/{id}` | `Operation`; result is a reconciliation report |
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

## Certificates

Issuing, renewing and reconciling certificates wait on the CA, DNS and HTTP
preflight probes, so they run on the worker. Admission checks only local state:
an unknown site is `404`, a request with no email when none is configured is
`409`, and a renewal naming a site with no managed certificate is `404`.
Preflight itself runs in the job, so a blocked check fails the operation and
its reason is in `error`.

One request per site, one renewal sweep and one reconciliation may be queued or
running at a time; another submission while one is in flight is `409`. A
renewal sweep stops between sites once `certificate_renewal_budget_seconds` has
passed and reports the rest as `skipped`.

A succeeded reconciliation whose result names a `deployment` has queued it;
poll `/v3/deployments/{deployment.id}` using the deployment status rules above.
Issuing and renewing never deploy.

The CLI (`blitzecdn cert request`, `cert renew`, `cert reconcile`) runs the same
work in-process and waits for it.
