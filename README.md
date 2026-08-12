# BlitzeCDN

BlitzeCDN is a small, security-focused control plane for converging Nginx CDN
edge servers. Python owns validation, desired state, deployment history,
planning, rollback, audit records, and process execution. Ansible exclusively
owns remote Linux state.

The project is intentionally opinionated: Debian 12+ and Ubuntu 24.04+ edges,
OpenSSH host-key verification, public-key-only SSH, non-root SSH users with
explicit sudo, UFW,
Fail2Ban, centrally coordinated ACME HTTP-01 issuance, managed certificate
uploads, and existing TLS certificate paths. Certificates are issued once by
the controller and distributed to every edge.

## Install

Python 3.12–3.14 is supported; development and CI currently use Python 3.14.
Certbot must also be installed on the controller for ACME requests.

### Standalone server

To run an independent control plane and edge on the same Debian 13+ or Ubuntu
24.04+ server, clone the release into the production path and run the standalone
installer:

```bash
sudo git clone --branch v2.0.0 --depth 1 \
  https://github.com/misaf/blitze-cdn-cp.git /opt/blitzecdn
sudo /opt/blitzecdn/install.sh standalone \
  --admin-cidr 203.0.113.8/32 \
  --public-address 203.0.113.10 \
  --email admin@example.com
```

Replace the CIDR with the network from which SSH administration is allowed and
the public address with the A/AAAA answer that reaches this edge. Supplying the
public address is important when the standalone server is behind NAT or uses
split DNS.
The installer creates the service accounts, local SSH trust, API credential,
inventory, systemd services, certificate timers, and a global `blitzecdn`
command. It deliberately does not deploy on the first run: add or recreate the
desired sites, review `blitzecdn plan`, then run `blitzecdn deploy`.
It is safe to rerun and does not replace existing API credentials or state.
The API remains bound to loopback; connect without opening another public port:

```bash
ssh -L 8000:127.0.0.1:8000 OPERATOR@EDGE_ADDRESS
```

Update an existing standalone installation to the newest stable release:

```bash
sudo /opt/blitzecdn/install.sh update --check
sudo /opt/blitzecdn/install.sh update
```

The updater accepts `--version vX.Y.Z` to select an exact release and `--yes`
for unattended confirmation. It backs up `.state`, local environment settings,
and `/etc/blitzecdn`, preserves credentials and desired state, validates the
updated service with `blitzecdn doctor`, and never deploys edge configuration.
Review `blitzecdn plan` and run `blitzecdn deploy` separately after an update.

Every standalone server has its own desired state and credentials. It does not
replicate changes to another standalone server.

Rebuild the running release as if on a brand-new server, or remove every
artifact the installer owns, without touching packages or unrelated files:

```bash
sudo /opt/blitzecdn/install.sh --fresh [--yes] [STANDALONE OPTIONS]
sudo /opt/blitzecdn/install.sh --uninstall [--yes]
```

`--fresh` reads the release tag from the current checkout, removes every
BlitzeCDN artifact, clones that same release from the upstream origin, and runs
the standalone installer with the options you pass. The checkout must be a Git
clone of the upstream repository. `--uninstall` only removes; it is safe to
rerun and keeps working even when `/opt/blitzecdn` was already deleted. Both
ask for confirmation unless `--yes` is given.

### Controller-only installation

```bash
./install.sh
```

The installer verifies Python, creates the private environment, installs the
pinned Ansible collection into `.state/collections`, and runs initial setup.
Setup never overwrites an existing `.env` or inventory, so re-running it is
safe.

To provision a whole server instead — control plane and edge on the same host —
clone a release to `/opt/blitzecdn` and run the standalone installer:

```bash
sudo git clone --branch vX.Y.Z https://github.com/misaf/blitze-cdn-cp.git /opt/blitzecdn
sudo /opt/blitzecdn/install.sh standalone --admin-cidr 203.0.113.8/32 --email ops@example.com
```

The path is not a suggestion: the hardened systemd units name `/opt/blitzecdn`,
and `update` and `--fresh` need a real clone with the right origin, so an
unpacked tarball elsewhere will not do.

It runs as root and provisions the host — a service account, a sudo rule, SSH
keys, public services — so read `install.sh` before you run it. Upgrade later
with `sudo /opt/blitzecdn/install.sh update`.

Environment variables that change what it installs, or how a later deploy
behaves:

| Variable | Effect |
| --- | --- |
| `BLITZECDN_DEV=1` | Install the `dev` dependency group too: test and lint tooling, and `src/` edits take effect without reinstalling. |
| `BLITZECDN_WRAPPER_DIR` | Where to put the `blitzecdn` command (default `~/.local/bin`). |
| `BLITZECDN_USER_WRAPPER=0` | Do not put `blitzecdn` on `PATH`; use `.venv/bin/blitzecdn`. |
| `BLITZE_ALLOW_EMPTY_SITES=true` | Explicitly permit a deployment to remove the final previously managed sites. Leave unset during normal operation. |

A lab controller wants the first:

```bash
BLITZECDN_DEV=1 ./install.sh
```

The Ansible roles live in `ansible/roles/` and are read at deploy time, so a
role edit takes effect immediately — there is nothing to rebuild, pin, or
reinstall after changing one.

An empty desired state is safe on a fresh edge. On an edge whose managed-site
registry is non-empty, it is refused by default because it would delete every
managed Nginx configuration and managed certificate. Recreate the existing
sites in desired state first. Use `BLITZE_ALLOW_EMPTY_SITES=true blitzecdn
deploy` only when removing the final sites is intentional.

Add an edge:

```bash
.venv/bin/blitzecdn edge add edge-01 \
  --host 192.0.2.10 \
  --public-address 203.0.113.10 \
  --user deploy \
  --ssh-source 198.51.100.0/24
```

`--host` is the SSH route. `--public-address` is the address public CDN DNS
answers with; omit it only when that is the same as `--host`, and repeat it for
multi-address edges. Keeping these separate supports NAT and split DNS without
local resolver overrides. Replace the example addresses and trusted management
network as appropriate. Verify every SSH fingerprint through a
trusted channel and add it to the controller's `known_hosts`. Use an SSH agent
or a key path outside this repository. Never disable host-key checking.

For an existing edge, replace its public address set without changing its SSH
route:

```bash
blitzecdn edge update edge-01 --public-address 203.0.113.11
```

SSH is public-key only in both directions. The controller connects with
`PreferredAuthentications=publickey`, `PasswordAuthentication=no`, and
`BatchMode=yes`, so a deploy fails immediately rather than falling back to a
password or blocking on a prompt — use an SSH agent for key passphrases. The
first deploy then installs the matching policy on the edge itself
(the `blitzecdn_sshd` role), which refuses to disable passwords unless
the account it would leave behind already has a working `authorized_keys`.
Install the operator key on a new edge before adding it here.

## Architecture

The package is layered, and the layering is checked rather than trusted:

| Layer | Module | Rule |
| --- | --- | --- |
| Domain | `domain/` | Knows only itself. No I/O, no adapter imports. |
| Ports | `ports.py` | `Protocol` interfaces over the outside world. May see the domain. |
| Application | `application/` | Orchestrates the domain through those ports. Never names a concrete adapter. |
| Infrastructure | `infrastructure/` | SQLite, Ansible, Certbot, filesystem, inventory, DNS. Matched to ports structurally — it never imports them. |
| Composition root | `control_plane.py` | The one module that knows both halves. Builds the adapters and injects them into the services. |
| Entry points | `cli/`, `api.py` | Call the `ControlPlane` facade. |

`tests/test_layering.py` walks the real source tree and fails when `domain/` or
`application/` imports an adapter package — fastapi, typer, sqlite3,
subprocess, yaml, cryptography, dns, and the rest. This used to be a convention
kept by review, and review misses a single import; the failure is invisible
until someone tries to test a service without a database.

`ControlPlane` owns no logic. Every method delegates to the service that does,
so each service declares only the narrow ports it actually uses while the CLI
and the API keep a single object to call.

## Configuration

Precedence is explicit:

1. CLI arguments affect only the requested invocation.
2. `BLITZE_*` environment variables configure the control plane.
3. Inventory/group/host variables describe environment policy.
4. Namespaced role defaults provide non-secret implementation defaults.

Fleet-wide, non-secret Ansible policy is stored in the control-plane database.
Manage overrides with `blitzecdn config set/list/unset`; the dynamic inventory
plugin publishes them to every edge. Shipped defaults remain in
`ansible/inventory/group_vars/`, but do not edit those tracked files. Secrets
remain in `.env` and edge-specific SSH policy remains on each edge record.

```bash
blitzecdn config set blitzecdn_base_timezone Asia/Tehran
blitzecdn config set blitzecdn_firewall_enabled true
blitzecdn config list
blitzecdn config unset blitzecdn_firewall_enabled
```

The CLI automatically loads `.env` from the project directory as local
defaults; already-exported variables take precedence. Production services can
instead use a systemd `EnvironmentFile` or secret manager. API secrets must be
at least 32 characters. Named credentials use
`BLITZE_API_KEYS=alice:secret,bob:secret`. Store Ansible secrets in
Vault-encrypted vars or an external secret plugin.

Non-secret defaults can be copied from
[`blitzecdn.example.toml`](blitzecdn.example.toml) to `blitzecdn.toml`.
Environment variables override matching TOML fields; secrets are deliberately
not valid TOML keys. Important environment variables are documented in
[.env.example](.env.example). Runtime state, generated Ansible vars, locks, and
SQLite data live under `.state/` by default and are ignored by Git.

## Common workflows

```bash
blitzecdn doctor
blitzecdn domain add example.com
blitzecdn record add example.com cdn \
  --value origin.example.com --proxied
blitzecdn site list
blitzecdn deploy
blitzecdn status
blitzecdn audit
blitzecdn rollback DEPLOYMENT_ID --yes
```

The full command surface:

| Group | Commands |
| --- | --- |
| Deployment | `validate`, `plan`, `deploy`, `status`, `drift`, `rollback` |
| Diagnostics | `doctor`, `audit`, `stats` |
| Setup | `init`, `setup`, `serve` |
| Edges | `edge list/add/update/remove`, `origin check` |
| Zones | `domain add/list/remove`, `record add/list/proxy/firewall/remove`, `dns export`, `site list/show` |
| Certificates | `cert list`, `cert preflight`, `cert renew`, `cert reconcile` |
| Cache | `cache purge` |

Sites are derived, not authored: `site` is read-only, and virtual hosts follow
from the proxied records under a domain.

Interactive `deploy` validates configuration, previews changes in Ansible check
mode, and asks before applying them. The individual `validate` and `plan`
commands remain available for automation and troubleshooting. Use `deploy
--yes` only after separate review in non-interactive automation. Exit codes are
0 success, 2 invalid input, 3 invalid configuration, 4 conflict, and 5 failed
deployment.

The CLI is synchronous: `deploy`, `plan`, and `rollback` return when Ansible
does.

## API

```bash
blitzecdn serve --host 127.0.0.1 --port 8000
```

For production, install `packaging/systemd/blitzecdn-api.service`. It supplies
the virtualenv `PATH`, keeps the API and certificate reconciler running, and
loads secrets from `/etc/blitzecdn/blitzecdn.env` when present. Put an
authenticated TLS reverse proxy in front of the loopback listener.

Unlike the CLI, the API does not block on a convergence. A run can take
`deployment_timeout_seconds`
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
`DESIRED_STATE_VERSION` in `src/blitzecdn/domain/sites.py` when the
`blitzecdn_nginx_sites` shape changes in a way an older role cannot honour, and
add the new version to `blitzecdn_nginx_supported_state_versions` in the Nginx
role — adding to that list rather than replacing it, so an edge converged by an
earlier release keeps working. The current schema is version 2, and the role
supports 1 and 2.

`tests/test_contract.py` enforces the boundary: it renders desired state from
real models and checks it against the role's `argument_specs.yml`, then renders
`site.conf.j2`. Run it after any change to `CdnSite` or the Nginx role.

`--no-cov` because the suite fails under 85% coverage by default, which no
single file reaches on its own.

```bash
pytest tests/test_contract.py --no-cov
BLITZECDN_UPDATE_FIXTURE=1 pytest tests/test_contract.py --no-cov  # after an intended change
```

These tests skip silently when the collection is not installed. Run
`./install.sh` first, and check the count: thirty-one tests, not thirty-one
skips.

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
serve the challenge path. Confirm those steps landed before involving a CA:

```bash
blitzecdn cert preflight example-cdn
```

It resolves each hostname against the inventory's explicit public edge
addresses (falling back to the SSH host for older inventories), reads CAA, and
checks that the site is in the last successful deployment, exiting `3` and naming
what to fix otherwise.

Those lookups go through the controller's own resolver by default. Set
`BLITZE_PREFLIGHT_DNS_SERVERS=1.1.1.1,1.0.0.1` to ask public resolvers instead,
which is the right answer wherever this host resolves differently from the
public internet — a split-horizon zone, an internal forwarder, or a transparent
proxy that claims every hostname. The last of those is worth knowing about: it
looks healthy for browsing and package installs, and turns every preflight into
a blocking failure that blames the zone. `blitzecdn doctor` probes for exactly
that and says which resolver it asked.

Then request a certificate and deploy again:

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

The request runs the same preflight checks and returns `409` without contacting
the CA if one blocks, so a rate-limited request is not spent on an attempt that
would fail. `{"skip_preflight": true}` issues anyway and is recorded in the audit
trail; it is for the case the controller cannot see, such as split-horizon DNS.
Renewal runs the checks too, with no override.

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

Dependencies are managed with [uv](https://docs.astral.sh/uv/) and the tasks
with [just](https://just.systems). `just install` builds the environment from
`uv.lock`; `pre-commit install` adds the formatting, shell, and hygiene hooks
on every commit.

`just check` runs every gate CI runs, in the same order — the workflow calls
these same recipes rather than repeating the commands, so a green local run
means a green pipeline:

```bash
just install
just check
```

The gates individually, when you want one of them:

```bash
just lint          # ruff format --check, then ruff check
just types         # mypy, strict
just test          # fails under 85% coverage
just shell-lint    # install.sh runs as root, so it is linted like the Python
just ansible-check # yamllint, playbook syntax, ansible-lint
just audit         # bandit and pip-audit
just docs-check    # the published reference still describes this control plane
```

`just docs-check` reads the documentation site from `../blitze-cdn-web`, or from
a path given as its argument, and fails when a route, model, CLI command,
setting, or environment variable here has no counterpart on its reference page —
or when a documented example no longer parses as the model it illustrates. It
skips when that checkout is absent; CI checks it out and does not.

`just test` passes its arguments through, so a single case is:

```bash
just test tests/test_domain.py -k some_case --no-cov
```

Dependencies live in `pyproject.toml` and are pinned in `uv.lock`, which is
committed. After editing either, `just lock` re-resolves and `just lock-check`
is the CI gate that fails when the two have drifted apart.

Ansible. Both variables matter: without `ANSIBLE_CONFIG` the collection path
and connection settings are not picked up, and `ANSIBLE_LOCAL_TEMP` keeps
scratch files inside the ignored `.state/`.

```bash
export ANSIBLE_CONFIG=ansible/ansible.cfg
export ANSIBLE_LOCAL_TEMP=.state/ansible-local
yamllint .
# `-i` is omitted: ansible.cfg points at the blitzecdn inventory plugin, which
# reads the fleet from the control-plane database. A syntax check parses no
# hosts, so it works against an empty one.
ansible-playbook ansible/playbooks/edge.yml --syntax-check \
  --extra-vars @tests/fixtures/desired-state.yml
ansible-playbook ansible/playbooks/acme-challenge.yml --syntax-check
ansible-lint ansible/playbooks/edge.yml ansible/playbooks/acme-challenge.yml
```

Security and packaging:

```bash
bandit -c pyproject.toml -r src
pip-audit
python -m build
```

Releases are tagged `vX.Y.Z`, and CI refuses a tag that does not equal the
`version` in `pyproject.toml`.

## Related repositories

BlitzeCDN is two repositories:

| Repository | Owns |
| --- | --- |
| **blitze-cdn-cp** (this one) | Control plane: validation, desired state, history, rollback, audit, CLI and API. Also all Ansible — inventory, `ansible.cfg`, the playbooks, and every role in `ansible/roles/`: the ten that converge remote edges plus `blitzecdn_controlplane` for the controller's own host. |
| [blitze-cdn-web](https://github.com/misaf/blitze-cdn-web) | Documentation site. Its maintained reference pages are reviewed against this repository. |

Documentation prose and maintained reference pages live in **blitze-cdn-web**.
When a route, flag, setting, or role variable changes, review and update the
affected pages there in the same release workflow. CI enforces the coverage half
of that from both sides — `just docs-check` here, `npm run check:surface`
there — but only a person can notice that a description became wrong rather than
absent.

Report vulnerabilities privately to the repository maintainers. Do not open a
public issue containing exploit details or credentials; rotate any exposed key
immediately and review the audit and deployment history.
