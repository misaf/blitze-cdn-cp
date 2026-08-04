# BlitzeCDN

BlitzeCDN is a small, security-focused control plane for converging Nginx CDN
edge servers. Python owns validation, desired state, deployment history,
planning, rollback, audit records, and process execution. Ansible exclusively
owns remote Linux state.

The project is intentionally opinionated: Debian 12+ and Ubuntu 24.04+ edges,
OpenSSH host-key verification, non-root SSH users with explicit sudo, UFW,
Fail2Ban, centrally coordinated ACME HTTP-01 issuance, managed certificate
uploads, and existing TLS certificate paths. Certificates are issued once by
the controller and distributed to every edge.

## Install

Python 3.12–3.14 is supported; development and CI currently use Python 3.14.
Certbot must also be installed on the controller for ACME requests.

```bash
python3.14 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ansible-galaxy collection install -r ansible/requirements.yml
```

Create local configuration and inventory:

```bash
.venv/bin/blitzecdn init --output .env
cp ansible/inventory/hosts.example.yml ansible/inventory/hosts.yml
set -a; source .env; set +a
```

Edit `ansible/inventory/hosts.yml`. Verify every SSH fingerprint through a
trusted channel and add it to the controller's `known_hosts`. Use an SSH agent
or a key path outside this repository. Never disable host-key checking.

## Configuration

Precedence is explicit:

1. CLI arguments affect only the requested invocation.
2. `BLITZE_*` environment variables configure the control plane.
3. Inventory/group/host variables describe environment policy.
4. Namespaced role defaults provide non-secret implementation defaults.

Group variables live in `ansible/inventory/group_vars/`, beside the inventory
file. Ansible only auto-loads `group_vars` adjacent to the inventory or the
playbook, so a `group_vars/` directory anywhere else — including the repository
root or `ansible/` — is silently ignored and every setting in it falls back to
the role default.

The CLI does not automatically source `.env`; use your shell, systemd
`EnvironmentFile`, or a secret manager. API secrets must be at least 32
characters. Named credentials use `BLITZE_API_KEYS=alice:secret,bob:secret`.
Store Ansible secrets in Vault-encrypted vars or an external secret plugin.

Non-secret defaults can be copied from
[`blitzecdn.example.toml`](blitzecdn.example.toml) to `blitzecdn.toml`.
Environment variables override matching TOML fields; secrets are deliberately
not valid TOML keys. Important environment variables are documented in
[.env.example](.env.example). Runtime state, generated Ansible vars, locks, and
SQLite data live under `.state/` by default and are ignored by Git.

## Common workflows

```bash
blitzecdn doctor
blitzecdn site add --file examples/site.yml
blitzecdn site list
blitzecdn validate
blitzecdn plan
blitzecdn deploy --yes
blitzecdn status
blitzecdn audit
blitzecdn rollback DEPLOYMENT_ID --yes
```

`plan` runs Ansible check mode. `deploy` and applied rollback require
confirmation unless `--yes` is supplied. Use `--json` on read and deployment
commands in automation. Exit codes are 0 success, 2 invalid input, 3 invalid
configuration, 4 conflict, and 5 failed deployment.

The CLI is synchronous: `deploy`, `plan`, and `rollback` return when Ansible
does.

```bash
blitzecdn serve --host 127.0.0.1 --port 8000
```

The API is not. A convergence can run for `deployment_timeout_seconds`
(default 900), far longer than any HTTP client or reverse proxy will wait, so
`POST /v1/deployments` and `POST /v1/rollbacks` return `202 Accepted` with a
`queued` record and converge on a worker thread. Poll
`GET /v1/deployments/{id}` for the outcome. The record is committed to SQLite
before the worker starts, and startup marks orphaned `queued`/`running` records
`abandoned`, so a controller crash is visible rather than silent. Rejections
that can be determined up front — no rollback target, a deployment already
running — still fail synchronously with 4xx.

Only `GET /health` is public. Control routes live under `/v1` and require
`X-API-Key`. Ten failed authentications from one client address within a minute
return `429`; behind a reverse proxy every request appears to come from the
proxy, so this is a coarse backstop and real per-client limiting belongs in the
proxy. Put the API behind authenticated TLS if it is exposed beyond localhost.
Interactive Swagger documentation is available at `/docs`, ReDoc at `/redoc`,
and the OpenAPI schema at `/openapi.json`. These documentation routes describe
the API but do not bypass authentication on control operations.

## Control plane / edge contract

The control plane emits `blitzecdn_desired_state_version` with every deployment
and the Nginx role refuses any version it does not support, so a mismatched
pair fails before touching a host rather than partway through a rollout. Bump
`DESIRED_STATE_VERSION` in `src/blitzecdn/domain/models.py` when the
`blitzecdn_nginx_sites` shape changes in a way an older role cannot honour, and
add the new version to `blitzecdn_nginx_supported_state_versions`.

`tests/test_contract.py` enforces the boundary: it renders desired state from
real models and checks it against the role's `argument_specs.yml`, then renders
`site.conf.j2`. Run it after any change to `CdnSite` or the Nginx role.

```bash
pytest tests/test_contract.py
BLITZECDN_UPDATE_FIXTURE=1 pytest tests/test_contract.py   # after an intended change
```

## Certificates

There are three TLS modes:

- `existing` references certificate files already present on every edge.
  Deployments write certificate destinations as root, so `certificate_path` and
  `certificate_key_path` must sit under `/etc/blitzecdn/tls/`, `/etc/ssl/`, or
  `/etc/letsencrypt/`. Both the control plane and the Nginx role enforce this.
- `uploaded` accepts and validates a certificate chain and unencrypted private
  key through the authenticated API, then distributes them during deployment.
- `requested` runs Certbot centrally and synchronizes HTTP-01 challenges to all
  edges before storing the issued certificate for deployment.

For a new hostname, first create the site with `certificate_mode: disabled`,
point its A/AAAA or CNAME records at the edges, and deploy once so every edge can
serve the challenge path. Then request a certificate and deploy again:

```bash
curl --fail-with-body -X POST \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"email":"client@example.com"}' \
  http://127.0.0.1:8000/v1/sites/example-cdn/certificate/request

blitzecdn deploy --yes
```

If `email` is omitted, `BLITZE_ACME_DEFAULT_EMAIL` must be configured. HTTP-01
requires every requested hostname to resolve publicly to the edges and port 80
to be reachable. It does not support wildcard names.

Upload an existing PEM chain and unencrypted PEM private key with:

```bash
curl --fail-with-body -X POST \
  -H "X-API-Key: $API_KEY" \
  -F certificate=@fullchain.pem \
  -F private_key=@privkey.pem \
  http://127.0.0.1:8000/v1/sites/example-cdn/certificate/upload
```

The API returns certificate metadata but never returns private-key material.
Managed keys are stored beneath `.state/certificates` with mode 0600. Always
protect the API with authenticated TLS before accepting uploads over a network.

`uploaded` and `requested` are set by the upload and request endpoints, which
own the on-edge paths (`/etc/blitzecdn/tls/<site>/`). Setting either mode — or
redirecting those paths — through `POST`/`PATCH /v1/sites` is rejected.

## Origin DNS

By default each edge re-resolves origin hostnames every
`blitzecdn_nginx_resolver_valid` (30s) through
`blitzecdn_nginx_resolvers` (the systemd-resolved stub, `127.0.0.53`), so an
origin that changes address is picked up without a reload. Set
`blitzecdn_nginx_resolvers: []` to pin origin addresses at config-load time
instead, which is nginx's default behaviour and appropriate when origins have
static addresses and you do not want a resolver in the request path.

## Failure and recovery

- A filesystem lock permits one deployment at a time.
- Each run uses an immutable desired-state snapshot and bounded logs.
- Timeout and failed runs remain recorded and never become canonical rollbacks.
- Startup marks orphaned queued/running records `abandoned`.
- Rollback first converges the selected snapshot; canonical desired state changes
  only after Ansible succeeds.
- An invalid Nginx configuration fails `nginx -t` before reload, leaving the
  running worker configuration active. Correct desired state and deploy again.
- Ansible uses `serial: 25%` and `any_errors_fatal` to limit partial rollout.

SQLite and local locks support one control-plane node. Back up `.state/` before
controller maintenance. Restore the database and rerun `validate`/`plan` after
recovery. For active/active operation, replace SQLite and filesystem locking
with transactional shared infrastructure.

## Development

```bash
ruff format --check src tests
ruff check src tests
mypy src
pytest
ANSIBLE_LOCAL_TEMP=.state/ansible-local ansible-playbook \
  -i ansible/inventory/hosts.example.yml ansible/playbooks/edge.yml \
  --syntax-check --extra-vars @tests/fixtures/desired-state.yml
ANSIBLE_LOCAL_TEMP=.state/ansible-local ansible-playbook \
  -i ansible/inventory/hosts.example.yml \
  ansible/playbooks/acme-challenge.yml --syntax-check
ansible-lint ansible/playbooks/edge.yml ansible/playbooks/acme-challenge.yml
yamllint .
bandit -c pyproject.toml -r src
python -m build
```

## Documentation site

Prose and generated reference documentation live in `web/`, a Nextra site with
a landing page at `/` and docs under `/docs`.

```bash
cd web
npm ci
npm run generate   # refresh the generated reference from the source tree
npm run dev        # http://localhost:3000
npm run build      # static export to web/out
```

Everything under **Reference** — CLI, HTTP API, configuration, and Ansible role
variables — is generated by `web/scripts/generate_reference.py` from the OpenAPI
schema, the Typer command tree, `Settings.from_environment`, and each role's
`argument_specs.yml`. Do not edit those pages by hand. CI runs
`generate_reference.py --check` and fails when the committed output no longer
matches the source, so changing a route, flag, setting, or role variable
requires regenerating and committing the result.

See [architecture](web/src/content/architecture.mdx),
[operations](web/src/content/operations.mdx), [security policy](SECURITY.md),
and [contributing guide](CONTRIBUTING.md).
