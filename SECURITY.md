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

## The documentation site

`web/` builds a static documentation site. It is not part of the deployed
artifact: it ships nothing to edge servers, never runs on the controller, and
holds no credentials. Its Node dependency tree is therefore outside the trust
boundary that protects controller state, and `npm audit` in CI covers it
separately from `pip-audit`.

Its reference generators import the control plane to read the OpenAPI schema.
They must construct an explicit throwaway `Settings` — `create_app()` with no
arguments calls `Settings.from_environment()`, which would read the operator's
real environment and open the production SQLite database as a side effect of
building documentation.
