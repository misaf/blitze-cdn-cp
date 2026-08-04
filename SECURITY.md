# Security policy

Report vulnerabilities privately to the repository maintainers; do not open a
public issue containing exploit details or credentials. Rotate any exposed key
immediately and review audit/deployment history.

BlitzeCDN assumes a trusted controller, verified SSH host keys, protected state
directory, secure secret injection, and least-privilege deployment accounts.
The API key mechanism provides authentication and attribution, not fine-grained
authorization. Bind the API to localhost unless a TLS-authenticated reverse
proxy and network policy protect it. Swagger UI, ReDoc, and the OpenAPI schema
are intentionally available for operator integration; restrict them at the
reverse proxy as well when endpoint metadata must not be public.

Certificate paths are a privilege boundary. A deployment copies
`certificate_path` and `certificate_key_path` onto every edge as root, so both
the domain models and the Nginx role confine them to `/etc/blitzecdn/tls/`,
`/etc/ssl/`, and `/etc/letsencrypt/`, and reserve the managed `uploaded` and
`requested` paths to the certificate endpoints. Do not relax either check
independently; they are deliberately redundant.

The two halves of that check now ship separately — this repository and the
`blitzecdn.edge` collection — so they can only drift if someone relaxes one and
releases it. `tests/test_contract.py` reads the *installed* collection and
fails when what this control plane emits no longer matches what the pinned edge
version accepts.

## Consuming the edge collection

`ansible/requirements.yml` pins an exact `blitzecdn.edge` version rather than a
range. The desired-state document is a versioned contract; an unpinned
collection would let a control plane and its edges diverge with no diff to
review. Treat an edge upgrade as a reviewed change: bump the pin, run the
contract tests, then deploy.

If the two do diverge anyway, the failure is loud rather than silent. The
control plane stamps `blitzecdn_desired_state_version` on every deployment and
the Nginx role refuses any version outside its supported list, so a mismatched
pair stops before touching a host instead of part-way through a rollout.

## The documentation site

The documentation site is a separate repository and is not part of the deployed
artifact: it ships nothing to edge servers, never runs on the controller, and
holds no credentials. Keeping its Node dependency tree out of this repository is
deliberate — it stays outside the trust boundary that protects controller state.

Its reference generators import this control plane to read the OpenAPI schema.
They must construct an explicit throwaway `Settings` — `create_app()` with no
arguments calls `Settings.from_environment()`, which would read the operator's
real environment and open the production SQLite database as a side effect of
building documentation.
