# BlitzeCDN

BlitzeCDN is a small, security-focused control plane for converging Nginx CDN
edge servers. Python owns validation, desired state, deployment history,
planning, rollback, audit records, and process execution. Ansible exclusively
owns remote Linux state.

The project is intentionally opinionated: Debian 12+ and Ubuntu 24.04+ edges,
OpenSSH host-key verification, non-root SSH users with explicit sudo, UFW,
Fail2Ban, and existing TLS certificate paths. Automatic per-edge ACME issuance
is excluded because it is unsafe for an uncoordinated multi-edge cluster.

## Install

Python 3.12–3.14 is supported; development and CI currently use Python 3.14.

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

The optional API is synchronous by design so work is not falsely queued in a
process-local background task:

```bash
blitzecdn serve --host 127.0.0.1 --port 8000
```

Only `GET /health` is public. Control routes live under `/v1` and require
`X-API-Key`. Put the API behind authenticated TLS if it is exposed beyond
localhost. Interactive API documentation is disabled in production code.

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
ansible-lint ansible/playbooks/edge.yml
yamllint .
bandit -c pyproject.toml -r src
python -m build
```

See [architecture](docs/architecture.md), [operations](docs/operations.md),
[security policy](SECURITY.md), and [contributing guide](CONTRIBUTING.md).
