# BlitzeCDN

BlitzeCDN is a small, security-focused control plane for converging Nginx CDN
edge servers. Python owns validation, desired state, deployment history,
planning, rollback, audit records, and process execution. Ansible exclusively
owns remote Linux state.

The project is intentionally opinionated: freshly installed Ubuntu 26.04 LTS edges,
OpenSSH host-key verification, public-key-only SSH, non-root SSH users with
explicit sudo, UFW,
Fail2Ban, centrally coordinated ACME HTTP-01 issuance, managed certificate
uploads, and existing TLS certificate paths. Certificates are issued once by
the controller and distributed to every edge.

## Install

Python 3.12–3.14 is supported; development and CI currently use Python 3.14.
Certbot must also be installed on the controller for ACME requests.

### Standalone server

To run an independent control plane and edge on one host, start with a fresh
Ubuntu 26.04 LTS server, clone the release into the production path, and run the
standalone installer:

```bash
sudo git clone --branch 2.x \
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

Every standalone server has its own desired state and credentials. It does not
replicate changes to another standalone server.

Move the server onto a newer release, keeping everything it holds:

```bash
sudo /opt/blitzecdn/install.sh update [--ref REF] [--yes] [--no-backup]
```

`update` fetches from origin, backs up the database, stops the services,
fast-forwards the checkout, rebuilds the virtualenv from the new lockfile, and
converges the host — which installs any new units, migrates the schema, and
starts the services again on the new release. Without `--ref` it follows the
branch the checkout tracks; a release installation is detached at its tag and
must name what to move to, as in `--ref v2.1.0`. The database, certificates,
inventory, and the API credentials in `/etc/blitzecdn` all survive. It refuses
a checkout with local modifications, one that cannot fast-forward, and one
whose origin is not upstream; each refusal happens before anything stops, so a
refused update leaves a serving host serving.

Rebuild the running release as if on a brand-new server, or remove every
artifact the installer owns, without touching packages or unrelated files:

```bash
sudo /opt/blitzecdn/install.sh --fresh [--yes] [STANDALONE OPTIONS]
sudo /opt/blitzecdn/install.sh --uninstall [--yes]
```

`--fresh` preserves the current source line: it reclones an exact release tag,
keeps a `2.x` installation attached to that branch, or pins any other
development checkout to its exact commit. It removes every BlitzeCDN artifact
first — including the database, the certificates, and the API credentials — then
runs the standalone installer with the options you pass. It rebuilds a host; it
does not upgrade one, which is what `update` above is for. The
checkout must be a Git clone of the upstream repository. `--uninstall` only
removes. Both operations require the checkout's Ansible runtime because Ansible
is the single implementation of system teardown. Standalone uninstall invokes
the same `blitzecdn_teardown` role as normal edge decommissioning, then removes
controller-only units, accounts, and commands. Bash removes `/opt/blitzecdn`
only after the complete play succeeds. Both ask for confirmation unless
`--yes` is given.

### Controller-only installation

```bash
./install.sh
```

The installer verifies Python, creates the private environment, installs the
pinned Ansible collections into `.state/collections`, and then hands initial
setup and command-wrapper generation to the installed Python runtime. Setup never
overwrites an existing `.env`, so re-running it is safe.

To provision a whole server instead — control plane and edge on the same host —
clone the maintained `2.x` branch to `/opt/blitzecdn` and run the standalone
installer:

```bash
sudo git clone --branch 2.x https://github.com/misaf/blitze-cdn-cp.git /opt/blitzecdn
sudo /opt/blitzecdn/install.sh standalone --admin-cidr 203.0.113.8/32 --email ops@example.com
```

The path is not a suggestion: the hardened systemd units name `/opt/blitzecdn`,
and `--fresh` needs a real clone with the right origin, so an unpacked tarball
elsewhere will not do.

It runs as root, but Bash only establishes the Python/Ansible runtime and invokes
the control-plane and uninstall playbooks. Ansible owns accounts, sudo, SSH
trust, units, and other host state; the installed control-plane runtime owns local-edge
registration and the optional first deployment.

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
| Ports | `application/ports/` | Feature-owned `Protocol` interfaces over the outside world. `ports.py` is a compatibility facade. |
| Application | `application/` | Orchestrates the domain through those ports. Never names a concrete adapter. |
| Infrastructure | `infrastructure/` | SQLite, Ansible, Certbot, filesystem, inventory, DNS. Matched to ports structurally — it never imports them. |
| Composition root | `control_plane.py` | The one module that knows both halves. Builds the adapters and injects them into the services. |
| Entry points | `cli/`, `api/` | Call the `ControlPlane` facade. CLI commands and HTTP routers are split along matching feature boundaries. |

`tests/test_layering.py` walks the real source tree and fails when `domain/` or
`application/` imports an adapter package — fastapi, typer, sqlite3,
subprocess, yaml, cryptography, dns, and the rest. This used to be a convention
kept by review, and review misses a single import; the failure is invisible
until someone tries to test a service without a database.

`ControlPlane` owns no logic. Every method delegates to the service that does,
so each service declares only the narrow ports it actually uses while the CLI
and the API keep a single object to call.

Long-lived deployment snapshots carry an explicit schema version and retain a
decoder for the legacy unversioned format, so an upgrade cannot silently make a
successful deployment unusable as a rollback target. HTTP v1 request and core
resource response models live in versioned `api/v1_*` and `api/v2_*` modules;
domain models therefore do not define the public transport contract. The v1
contract remains available for existing clients while v2 evolves independently.
Ansible and inventory documents are
rendered by `infrastructure/ansible_mapping.py`, never by domain methods.

Application services receive small feature policies rather than the global
settings object. Scheduled cross-feature behavior is owned by
`application/maintenance.py`; Dramatiq actors only select and invoke a typed
maintenance operation.

## Edge runtime

A BlitzeCDN edge is a minimal Linux host that runs the CDN as containers. The
host keeps what only a host can own; everything BlitzeCDN serves traffic with
runs on Docker.

```text
BlitzeCDN control plane
        │  Ansible over SSH
        ▼
┌──────────────────────────────────────────────┐
│ Edge host                                    │
│                                              │
│ Host infrastructure                          │
│ ├── Ubuntu 26.04 LTS                         │
│ ├── SSH (blitzecdn_sshd, blitzecdn_fail2ban) │
│ ├── Docker Engine (blitzecdn_docker)         │
│ ├── ufw (blitzecdn_firewall)                 │
│ └── sysctl (blitzecdn_kernel)                │
│                                              │
│ Docker Compose (blitzecdn_edge_stack)        │
│ ├── blitzecdn-edge                           │
│ │   └── Nginx + GeoIP2 + Brotli + njs        │
│ │       HTTP/1.1, HTTP/2, HTTP/3             │
│ └── geoipupdate (one-shot, on a timer)       │
│                                              │
│ Persistent host state                        │
│ ├── /etc/nginx/{conf.d,sites-*}              │
│ ├── /etc/blitzecdn/{compose.yml,nginx,tls}   │
│ ├── /var/lib/blitzecdn/{acme,edge,empty}     │
│ ├── /var/cache/nginx/blitzecdn               │
│ ├── /usr/share/GeoIP                         │
│ └── /var/log/nginx                           │
└──────────────────────────────────────────────┘
```

Monitoring is deliberately absent. `node_exporter`, an Nginx exporter, Alloy,
Prometheus, Loki, Grafana and Alertmanager are Phase 2 and arrive as their own
containers. What they will read already exists: the `blitzecdn` access log
format carrying the per-request cache outcome, and the loopback-bound
`stub_status` endpoint.

### Host and container responsibilities

Nothing that serves CDN traffic is installed directly on the host. Nginx,
`libnginx-mod-http-geoip2`, `libnginx-mod-http-brotli-filter`,
`libnginx-mod-http-js` and `geoipupdate` live only in containers. The host owns
Docker Engine, the Compose plugin, `containerd`, SSH, `ufw`, `fail2ban`,
systemd-resolved, kernel tuning and the persistent bind-mount directories.

Everything else is unchanged. The control plane still renders every site from
the same models through the same templates onto the same paths; the container
mounts them. Origin proxying, SSL modes, automatic HTTPS, minimum TLS version,
uploaded and requested certificates, cache policy, compression, visitor
headers, GeoIP country rules, per-site firewall rules, Under Attack Mode,
HTTP/3, origin SNI, origin `Host`, the catch-all default server, ACME
challenges and the status endpoint all behave exactly as before.

### The image

```text
ghcr.io/misaf/blitzecdn-edge:<version>
```

Built from [`docker/edge/`](docker/edge/) on `ubuntu:26.04` with four Ubuntu
runtime packages. BlitzeCDN treats Nginx and its dynamic modules as one
ABI-compatible unit; Ubuntu expresses that by pinning every
`libnginx-mod-*` package to an `nginx-abi-<version>` virtual package, so apt
resolving all four in one transaction is the guarantee. The build then proves
it: it loads all three modules and uses a directive from each, so an image that
would fail `nginx -t` on a real edge fails in CI instead.

Publishing is a separate workflow from CI
([`.github/workflows/edge-image.yml`](.github/workflows/edge-image.yml)) and
never upgrades a fleet by itself. A build that ran is not a build any edge is
using until you say so.

Build it locally with `just edge-image`.

### Host networking

The edge container uses `network_mode: host`. Every per-site source rule, the
GeoIP2 country lookup and `BZ-Connecting-IP` read `$remote_addr`; behind
Docker's bridge that would be a gateway address, and an edge would apply a
country rule to itself. Host networking also means no NAT hop per packet and no
published-port list to keep in step with the supported ports.

All thirteen public TCP ports (80, 8080, 8880, 2052, 2082, 2086, 2095, 443,
2053, 2083, 2087, 2096, 8443) and UDP/443 for HTTP/3 work exactly as they did
natively.

### Persistent state

Containers are disposable; none of the following lives inside one.

| Path | Holds | Mounted |
| --- | --- | --- |
| `/etc/nginx/conf.d` | Cache, GeoIP, status and Under Attack drop-ins | read-only |
| `/etc/nginx/sites-available`, `sites-enabled` | Per-site virtual hosts and the catch-all | read-only |
| `/etc/nginx/blitzecdn-managed-sites` | Managed-site registry (controller-owned) | not mounted |
| `/etc/blitzecdn/compose.yml` | The Compose project | not mounted |
| `/etc/blitzecdn/nginx` | The Under Attack Mode njs module | read-only |
| `/etc/blitzecdn/tls` | Managed certificate chains and private keys | read-only |
| `/etc/blitzecdn/geoipupdate.env` | MaxMind credentials, `0600` root | updater only |
| `/var/lib/blitzecdn/acme` | ACME HTTP-01 challenge webroot | read-only |
| `/var/lib/blitzecdn/edge/image` | The deployed image, for rollback | not mounted |
| `/usr/share/GeoIP` | GeoLite2-Country database | read-only |
| `/var/cache/nginx/blitzecdn` | Response cache | read-write |
| `/var/log/nginx` | Access and error logs | read-write |

Only the cache and the logs are writable to the edge. An edge has no business
rewriting what the control plane rendered, and a container that cannot write
its own configuration cannot be talked into persisting a change the next
converge would silently revert. Private keys stay `0600` root in a `0700`
directory and are read by the container's Nginx master at start and reload.

Recreating or upgrading the container touches none of this.

### Deploying configuration

Unchanged:

```bash
blitzecdn record add example.com cdn --value origin.example.com --proxied
blitzecdn deploy
```

The converge renders the tree, validates it by running `nginx -t` in a throwaway
container with the same mounts and no network, and then signals the running
container to reload. It does not replace the container: doing so would drop
every connection in flight and empty the shared-memory cache zone for a change
Nginx applies without dropping a request.

If the rendered configuration fails `nginx -t`, the previous files are restored,
the rollback is re-validated, and nothing is reloaded. The edge keeps serving
what it was serving. A bad render costs a failed run, never an edge that cannot
restart.

### Upgrading the runtime

The image version is fleet policy, not desired state. A customer adding a
hostname must never be a candidate for an Nginx upgrade.

```bash
blitzecdn config set blitzecdn_edge_image_tag 2.8.0
blitzecdn config set blitzecdn_edge_image_digest sha256:...   # optional, better
blitzecdn deploy --limit edge-01                              # canary first
blitzecdn deploy
```

Never `latest`. A floating tag makes "which build is this fleet running"
unanswerable and rollback a guess. With a digest set, the pull, the
configuration test and the running container are provably the same bytes.

An upgrade pulls the image, pins it to its digest, validates the configuration
this edge is *currently serving* against the new image, replaces the container,
and then verifies it is serving. The play converges 25% of the fleet at a time
and `any_errors_fatal` stops the rollout on the first batch that fails, so a
broken build cannot reach the whole fleet.

### Rollback

Every successful converge records what it was asked for and what that resolved
to, in `/var/lib/blitzecdn/edge/image`. When a new image fails validation or
health, the edge is returned to that exact digest, restarted, and health-checked
again — and the run still fails, so nobody mistakes a recovered edge for a
successful upgrade. Restoring a *tag* would not do: by then it may point
somewhere else, and "put the old one back" would install a third unknown
version.

An edge that has never served has nothing to return to, and says so rather than
reporting itself rolled back.

### Health checks

"Running" is not health. Nginx keeps serving the configuration it loaded at
start, so a container can look fine while the tree on disk is broken. A converge
is not finished until:

- the container is running, and Docker's own `HEALTHCHECK` reports healthy —
  the configuration on disk parses, the master process is alive, and the
  loopback status endpoint answers;
- `nginx -t` succeeds inside the running container;
- every supported public TCP port accepts a connection;
- UDP/443 has a listener whenever HTTP/3 is enabled;
- the status endpoint returns 200;
- the GeoLite2 database is present whenever a site needs it.

The UDP check is what keeps the firewall and the QUIC listener from silently
disagreeing. The play already refuses to converge when the two desired states
differ; this catches a QUIC bind that failed inside the container, which would
otherwise leave UDP/443 open, `Alt-Svc` advertised, and nothing listening.

### GeoIP

Unchanged for operators, and still required in Phase 1. `BZ-IPCountry`,
country-based firewall rules and the capability validation all work as before,
and a site that asks for country filtering on an edge without GeoIP still fails
the deploy rather than quietly serving the traffic it was told to block.

Credentials stay in the controller's `.env`:

```bash
BLITZE_MAXMIND_ACCOUNT_ID=123456
BLITZE_MAXMIND_LICENSE_KEY=...
```

They are forwarded into the Ansible environment and written to a `0600`
root-owned `/etc/blitzecdn/geoipupdate.env` on the edge, which reaches the
updater container as an `env_file` — never as a Compose `environment:` entry,
where `docker inspect` would hand the key to anyone who can talk to the engine.
They are never baked into an image and never committed.

The updater is MaxMind's own container, pinned to a digest, run as a one-shot.
Its *schedule* stays a host responsibility, for the same reason the firewall and
the kernel do: a systemd timer gives the fleet a calendar and a randomised
delay, so a hundred edges do not arrive at MaxMind on the same second.

```bash
systemctl list-timers blitzecdn-geoipupdate.timer
systemctl start blitzecdn-geoipupdate.service     # refresh now
```

Nginx picks up a replaced database through `geoip2 auto_reload` with no reload.

### Cache

The cache is a host directory bind-mounted read-write into the container, so it
survives a reload, a container replacement and a runtime upgrade. `blitzecdn
cache purge` is unchanged: it computes the same MD5 cache-key paths under the
same directory and deletes the files, which is still the only way to purge with
open-source Nginx.

### Troubleshooting

```bash
docker compose --file /etc/blitzecdn/compose.yml ps
docker logs --tail 200 blitzecdn-edge
docker inspect -f '{{.State.Health.Status}}' blitzecdn-edge
docker exec blitzecdn-edge nginx -t
docker exec blitzecdn-edge nginx -T        # the whole assembled configuration
docker exec blitzecdn-edge nginx -s reload
cat /var/lib/blitzecdn/edge/image          # what this edge is running, exactly
tail -f /var/log/nginx/blitzecdn-access.log
```

The container's own output carries startup and error messages only; the request
log is the bind-mounted file above.

### Replacing an older edge host

BlitzeCDN 2.x requires fresh Ubuntu 26.04 LTS edge hosts. Existing
native-Nginx BlitzeCDN edges are not upgraded in place, and BlitzeCDN never
stops, disables or purges a pre-existing host Nginx. A converge fails safely
before preparing the runtime when `/usr/sbin/nginx` exists.

To move an older edge to 2.x:

1. Provision a fresh Ubuntu 26.04 host.
2. Register it as a new edge.
3. Deploy the desired state.
4. Validate traffic and TLS.
5. Shift traffic to the new edge.
6. Decommission the old host.

### Decommissioning

Stopping the runtime and destroying what it was serving are separate
operations, and `blitzecdn_teardown_remove_data` is the difference. The
containers always go: they are disposable, and a stopped edge is one deploy
away from serving again. TLS material, the configuration tree, ACME state, the
GeoIP database, the cache and the managed-site registry go only when that is
true, which is the default for `blitzecdn edge remove` and for the uninstaller
— both of which mean the host is gone. Logs survive even then, because a
decommissioned host is often decommissioned because something went wrong on it.

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
| Zones | `domain add/list/remove`, `record add/list/proxy/ssl/ssl-automatic/minimum-tls/always-use-https/under-attack/cache-query-string/firewall/remove`, `dns export`, `site list/show` |
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

For production, install both units in `packaging/systemd/`: the API unit runs
FastAPI and the lightweight scheduler, while the worker unit executes Dramatiq
deployments and scheduled certificate/drift work. Both supply the virtualenv
`PATH` and load secrets from `/etc/blitzecdn/blitzecdn.env` when present. Put an
authenticated TLS reverse proxy in front of the loopback listener.

Unlike the CLI, the API does not block on a convergence. A run can take
`deployment_timeout_seconds`
(default 900), far longer than any HTTP client or reverse proxy will wait, so
`POST /v2/deployments` and `POST /v2/rollbacks` return `202 Accepted` with a
`queued` record and converge in the Dramatiq worker. Poll
`GET /v2/deployments/{id}` for the outcome. The record is committed to SQLite
before the worker starts. Startup republishes `queued` records and marks only
orphaned `running` records `abandoned`, after acquiring the deployment lock so
it cannot rewrite work a live CLI or worker still owns. Rejections that can be
determined up front — no rollback target, a deployment already running — still
fail synchronously with 4xx.

Only `GET /health` is public. New integrations should use `/v2`; the frozen
`/v1` routes remain available for existing clients. Both versions require
`X-API-Key`. Ten failed authentications from one client address within a minute
return `429`; behind a reverse proxy every request appears to come from the
proxy, so this is a coarse backstop and real per-client limiting belongs in the
proxy. Put the API behind authenticated TLS if it is exposed beyond localhost.
Interactive Swagger documentation is available at `/docs`, ReDoc at `/redoc`,
and the OpenAPI schema at `/openapi.json`. These documentation routes describe
the API but do not bypass authentication on control operations.

The key is published as an OpenAPI security scheme — `ApiKeyAuth`, `type:
apiKey`, `in: header`, `name: x-api-key` — and every protected operation
references it, so Swagger UI's *Authorize* button and an imported Postman
collection both carry the credential at collection level instead of asking for
a header on each request. The schema deliberately contains no `servers` block
and no key value: paths are relative, so the base URL and the key are supplied
by whatever runs the requests (a Postman environment, `$API_KEY` in the curl
examples below).

## Control plane / edge contract

The `tests/test_contract.py` and `tests/test_ansible_*_contracts.py` suites enforce the boundary: they render desired state from
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

There are three certificate sources:

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
  http://127.0.0.1:8000/v2/sites/example-cdn/certificate/request

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
  http://127.0.0.1:8000/v2/sites/example-cdn/certificate/upload
```

The API returns certificate metadata but never returns private-key material.
Managed keys are stored beneath `.state/certificates` with mode 0600. Always
protect the API with authenticated TLS before accepting uploads over a network.

`uploaded` and `requested` are set by the upload and request endpoints, which
own the on-edge paths (`/etc/blitzecdn/tls/<site>/`). Setting either mode — or
redirecting those paths — through `POST`/`PATCH /v2/sites` is rejected.

## SSL modes

Each proxied record has one Cloudflare-style `ssl_mode` controlling both sides
of the edge connection:

| Mode | Visitor to edge | Edge to origin | Origin certificate |
| --- | --- | --- | --- |
| `off` | HTTP only | HTTP | Not applicable |
| `flexible` | HTTP or HTTPS | HTTP, except HTTPS on the alternate HTTPS ports | Not verified |
| `full` | HTTP or HTTPS | Mirrors the visitor | Not verified |
| `full_strict` | HTTP or HTTPS | Mirrors the visitor | Verified against the system trust store and origin SNI, on the HTTPS leg |

Off never encrypts the origin leg. Full and Full (strict) mirror the visitor: an
HTTPS request is proxied over HTTPS, and a request that arrived on one of the
HTTP listeners is proxied over HTTP, because neither mode promises to
re-originate a plaintext request as TLS. Full (strict) verifies the origin
certificate wherever that leg is HTTPS, and has nothing to verify where it is
not. In every mode the origin port is the visitor's own — see below.

Flexible is the one mode whose origin leg also depends on the listener port,
matching Cloudflare: Flexible applies to HTTPS on port 443 only, and HTTPS on
the five alternate HTTPS proxy ports falls back to Full-like transport.

| Visitor | Flexible edge to origin |
| --- | --- |
| HTTP on any HTTP proxy port `P` | `http://origin:P` |
| HTTPS on 443 | `http://origin:443` |
| HTTPS on 2053, 2083, 2087, 2096 or 8443 | `https://origin:` that same port |

Only the transport falls back. The fallback leg behaves like Full and never
like Full (strict): it sends SNI and does not verify the origin certificate, so
enabling Flexible never starts demanding a valid certificate from an origin that
was never asked for one.

New records start Off and are enrolled in Cloudflare-compatible Automatic
SSL/TLS (`ssl_automatic_mode: auto`). Uploading or issuing a certificate does
not itself change their SSL mode. After unattended first-certificate issuance,
the reconciler immediately runs the automatic scanner; it also runs
approximately monthly. The scanner probes both the current transport and HTTPS
from every edge, compares their responses, and may upgrade the record to the
strongest compatible mode.
It never downgrades a record. Missing edges, 5xx responses, failed TLS, or
different HTTP/HTTPS content abort the upgrade.

Select a mode directly when needed:

```bash
blitzecdn record ssl example.com cdn --mode full_strict
blitzecdn deploy
```

As with Cloudflare, switch Automatic SSL/TLS to Custom when the mode must stay
entirely under operator control:

```bash
blitzecdn record ssl-automatic example.com cdn --mode custom
blitzecdn record ssl example.com cdn --mode off
blitzecdn deploy
```

The equivalent record API field accepts only `auto` or `custom`. A manual scan
uses the same workflow as the scheduler and deploys once if it upgrades any
records:

```bash
blitzecdn ssl reconcile

curl --fail-with-body -X POST \
  -H "X-API-Key: $API_KEY" \
  http://127.0.0.1:8000/v2/ssl/automatic/reconcile
```

Flexible, Full and Full (strict) require an active edge certificate. Changing a
site back to Off stops its HTTPS listener but retains the installed certificate
and renewal state, so a secure mode can be re-enabled without another issuance.
Full is intended only for origins whose TLS certificate cannot be validated;
it still requires a successful TLS handshake. No mode falls back automatically
when an origin is unavailable or fails TLS.

The edge preserves the listener port when connecting to the origin. A request
received on port 80 reaches origin port 80, one received on 8443 reaches origin
port 8443, and the same rule applies to every supported proxy port. The SSL
mode, the visitor's own protocol and — under Flexible — the listener port
together select HTTP or HTTPS for that connection; records do not configure a
separate origin port. So a Flexible site answering on 8443 proxies to
`https://origin:8443`, the same site answering on 443 proxies to
`http://origin:443`, and any of the four modes answering on 8080 proxies to
`http://origin:8080`.

The origin only has to serve the ports its visitors actually use. Certificate
preflight and `blitzecdn origin check` probe one endpoint per site — port 80 for
Off and Flexible, port 443 for Full and Full (strict) — rather than requiring an
answer on all thirteen public proxy ports, so an origin listening on 80 and 443
deploys normally. A request arriving on an alternate port the origin does not
serve gets a 502 from the edge; the rest of the site is unaffected.

That gap is widest under Flexible, deliberately: its canonical probe is
`http://origin:80`, while its alternate HTTPS listeners use `https://origin:`
their own port. Probing those too would demand TLS on five extra ports from
every origin that enables Flexible, so a request on 2053 to an origin that does
not serve it 502s on its own without failing the deployment.

TLS-enabled sites serve both HTTP and HTTPS by default. Enable Always Use HTTPS
to redirect visitor HTTP requests while leaving the selected origin encryption
mode unchanged:

```bash
blitzecdn record always-use-https example.com cdn --on
blitzecdn deploy
```

Use `--off` to serve both schemes again. The equivalent API update is `PATCH
/v2/domains/{domain}/records/{name}?type=A` with
`{"always_use_https": true}` or `false`. The ACME challenge path remains
available over HTTP in either mode.

The redirect is a Cloudflare-compatible permanent `301` to the same host and
URI over HTTPS, sent before anything is proxied, on every HTTP listener and on
the Under Attack Mode challenge endpoints alike. It carries no port. The
thirteen public proxy ports are two independent sets rather than seven pairs —
8080 has no HTTPS counterpart, and Cloudflare publishes no mapping between them
— so a request to `http://example.com:8080/path` is redirected to
`https://example.com/path` on the default 443, which is a listener every
TLS-enabled site serves. Inventing a port mapping is the one way this redirect
could loop or land on a closed port, so it does not.

Always Use HTTPS is inert while `ssl_mode` is Off, matching Cloudflare, which
removes the control from its dashboard for an Off zone. Off serves no HTTPS
listener, so redirecting to one would send every visitor to a port the edge does
not answer on — and a `301` is cached by browsers, which makes that worse than a
temporary mistake. The record API still accepts the two settings in either
order and keeps the stored preference: it starts redirecting the moment a secure
mode is selected, and stops again if the mode returns to Off. No combination is
rejected, and nothing is silently rewritten.

The minimum visitor protocol defaults to TLS 1.2. A hostname that only serves
modern clients can require TLS 1.3 independently of its origin SSL mode:

```bash
blitzecdn record minimum-tls example.com cdn --version 1.3
blitzecdn deploy
```

Use `--version 1.2` to restore TLS 1.2 and 1.3 compatibility. The equivalent
record API field is `minimum_tls_version`, accepting `"1.2"` or `"1.3"`.

## Under Attack Mode

Under Attack Mode is BlitzeCDN's emergency edge challenge/mitigation mode. It
intercepts requests before cache lookup or origin proxying and requires
unverified browsers to complete a short SHA-256 proof of work. The edge then
issues a signed, host-only `blitzecdn_clearance` cookie for 1800 seconds. This
is not Cloudflare's global Managed Challenge intelligence: there is no bot
score, reputation feed, CAPTCHA, automatic detection, or per-path policy.

Enable the fleet capability once, provision its HMAC secret in the control
plane's 0600 `.env`, then turn the policy on for a record:

```bash
blitzecdn config set blitzecdn_nginx_under_attack_enabled true
printf 'BLITZE_UNDER_ATTACK_SECRET=<at-least-32-random-bytes>\n' >> .env
blitzecdn record under-attack example.com cdn --on
blitzecdn deploy
```

Use a securely generated secret and keep the same value on every edge. The
secret is environment-only: it is not accepted by `blitzecdn config set`,
TOML, desired-state snapshots, or API models. A site requesting the mode while
the capability is disabled or the secret is absent fails preflight before the
edge configuration changes. The managed Ubuntu stack supplies
`libnginx-mod-http-js` for the njs implementation.

The v2 record field is `under_attack_mode: boolean`; PATCH the usual
`/v2/domains/{domain}/records/{name}?type=A` route with
`{"under_attack_mode": true}`. HTTP v1 is frozen and neither reports nor
accepts the field, while v1 reads continue to work when the underlying record
has it enabled.

Challenges are stateless HMAC-SHA-256 tokens, expire after five minutes, and
bind the proof and clearance to the request host, client IP (IPv4 or IPv6), and
User-Agent. The proof requires 16 leading zero bits of SHA-256 work. Clearance
cookies are `HttpOnly`, `SameSite=Lax`, `Path=/`, and `Secure` when issued over
HTTPS. A TLS site using Always Use HTTPS moves the challenge itself to HTTPS
before setting the cookie. Changing address or User-Agent can therefore
require a fresh proof. Because the design is stateless, a captured challenge
can be replayed for its remaining five-minute lifetime and a captured clearance
for its remaining cookie lifetime, but only from the same host/IP/User-Agent
binding; there is deliberately no per-request session database in this release.

`/.blitzecdn`, `/.blitzecdn/challenge`,
`/.blitzecdn/challenge/verify`, and the whole `/.blitzecdn/` namespace are
reserved for the edge and never proxy to customer origins or enter the CDN
cache. `/.well-known/acme-challenge/` remains a higher-priority local location
and bypasses both firewall and mitigation logic.

API clients and other non-browser clients do not receive an exemption: without
a valid cookie they receive the challenge redirect and responses marked
`X-BlitzeCDN-Mitigation: challenge`. POST and other non-idempotent requests are
resumed as a browser GET after verification, so enable this emergency mode
only when that tradeoff is acceptable, and disable it once the incident ends.

## HTTP/3 (QUIC)

HTTP/3 is an opt-in per-record visitor protocol. It applies only from visitors
to a BlitzeCDN edge; proxying from the edge to the origin remains HTTP/1.1 over
the transport selected by `ssl_mode`. Enable it for a TLS-enabled record with:

```bash
blitzecdn record http3 example.com cdn --on
blitzecdn deploy
```

The v2 API field is the boolean `http3_enabled`. Its migration-safe default is
`false`, and setting it while `ssl_mode` is `off` is rejected. QUIC always uses
TLS 1.3 internally, but `minimum_tls_version: "1.2"` remains valid and preserves
TLS 1.2 support for TCP clients.

The initial implementation listens for QUIC only on UDP/443. Alternate HTTPS
ports (2053, 2083, 2087, 2096, and 8443) remain TCP-only. HTTP/2 and HTTP/1.1
stay available on TCP/443 as fallbacks. Enabled sites advertise
`Alt-Svc: h3=":443"; ma=86400`; disabling the setting removes both that header
and the managed UDP/443 firewall rule when no enabled site still needs it.

UDP/443 must be reachable end to end. BlitzeCDN owns the edge Nginx stack as a
container image built from Ubuntu's `nginx`, `libnginx-mod-http-geoip2`,
`libnginx-mod-http-brotli-filter` and `libnginx-mod-http-js` packages, resolved
in one apt transaction so they share an `nginx-abi`. Before any firewall or
live configuration change, the converge starts a throwaway container from that
image and requires Nginx 1.25.0+, `--with-http_v3_module`, an `--build=Ubuntu`
binary, loadable dynamic modules and accepted Brotli and njs directives. It
fails instead of silently dropping a capability. The static Brotli module is not
installed because BlitzeCDN emits no `brotli_static` directive.

Do not build an edge image that mixes nginx.org packages with Ubuntu dynamic
modules. For an existing fleet, provision fresh Ubuntu 26.04 edges, validate a
small batch, then shift traffic and enable HTTP/3 per site — see
[Edge runtime](#edge-runtime).

The complete clean-machine proof is intentionally separate from fast tests:

```bash
just test-integration-http3
```

It provisions `ubuntu:26.04`, runs the real containerised edge on it, and makes
real HTTP/1.1, HTTP/2, HTTP/3-only, GeoIP2, Brotli and Under Attack Mode
requests against two QUIC sites — then proves a configuration change reloads
without replacing the container, an image upgrade preserves the cache and the
TLS material, a broken image is rolled back to the previous digest, the stack
returns after the engine restarts, and a runtime teardown does not destroy
customer state. To verify a deployed edge manually, use an HTTP/3-capable
client:

```bash
curl --http3-only -I https://cdn.example.com/
```

## WebSockets and cache query strings

WebSocket proxying is automatic for every proxied hostname. The edge forwards
the HTTP Upgrade handshake and bypasses the response cache for upgraded
connections; no per-record switch is needed. Long-lived applications should
send heartbeat traffic within the configured Nginx proxy read timeout.

Cache keys include the full query string by default, so `/asset?v=1` and
`/asset?v=2` are different objects. For content whose query parameters never
change the response, collapse every query variant onto the same raw path:

```bash
blitzecdn record cache-query-string example.com cdn --mode ignore
blitzecdn deploy
```

Use `--mode include` to restore the safe default. Ignore mode changes only the
cache key: the full original query still reaches the origin on a MISS, and a
named purge automatically removes the path-only cache entry. Do not use ignore
mode when query parameters select content, authentication, language, or user
state. The equivalent API field is `cache_query_string_mode`, accepting
`"include"` or `"ignore"`.

## Response compression

Every proxied hostname is compressed at the edge. `brotli` is the default: it
offers Brotli to clients that accept it and gzip to the rest.

```bash
blitzecdn record compression example.com cdn --mode gzip
blitzecdn deploy
```

`--mode` takes `brotli`, `gzip`, or `off`. The API field is `compression` on
**v2 only** — v1 is frozen and neither reports nor accepts it, so a v1 client
reading the record sees the same shape it always did.

Three things worth knowing before changing it:

- **Brotli is part of the managed edge image.** Ubuntu 26.04 splits its dynamic
  modules, so the image installs `libnginx-mod-http-brotli-filter` for the exact
  Nginx ABI. If it is missing or unloadable, provisioning fails; Brotli policy
  is never silently changed to gzip. The static module is not needed.
- **`off` means this edge does not compress, not that responses arrive
  uncompressed.** A body the origin already encoded is passed through under
  every mode; nginx will not decode and re-encode one to strip it.
- **Compression runs on cache hits too.** The cache stores what the origin
  sent and the filter runs on the way out, so `blitzecdn_nginx_gzip_comp_level`
  and `blitzecdn_nginx_brotli_comp_level` (both 5) are per-response costs, not
  per-MISS costs. `blitzecdn_nginx_compression_types` and
  `blitzecdn_nginx_compression_min_length` (256 bytes) bound what is worth
  spending them on.

Cache correctness needs no attention here: edges already collapse client
`Accept-Encoding` into `""`, `"gzip"` or `"br"` and make it part of the cache
key, so a compressed and an identity response cannot share one entry.

## Visitor request headers

An origin behind the CDN sees an edge address on every connection. These
headers are how it learns about the visitor instead. They are written by
BlitzeCDN on the request to the origin, in a namespace BlitzeCDN owns:

| Header             | Contains                                                          |
| ------------------ | ----------------------------------------------------------------- |
| `BZ-Connecting-IP` | The original visitor IP address, IPv4 or IPv6.                    |
| `BZ-IPCountry`     | The ISO 3166-1 alpha-2 country code derived at the BlitzeCDN edge — `DE`, `IR`, `US`. |

```bash
blitzecdn record visitor-headers example.com cdn --ip-country
blitzecdn deploy
```

`--connecting-ip/--no-connecting-ip` and `--ip-country/--no-ip-country` each
set one switch and leave the other alone. The API field is `visitor_headers` on
**v2 only** — v1 is frozen and neither reports nor accepts it — and a PATCH
replaces the whole block:

```json
{ "visitor_headers": { "connecting_ip": true, "ip_country": true } }
```

`connecting_ip` is on by default and `ip_country` is off, because the country
lookup needs a database the edge role does not install unless asked.

Three things worth knowing before trusting them at the origin:

- **`BZ-*` headers are generated and overwritten by BlitzeCDN.** The value
  comes from `$remote_addr` — the peer address of the connection nginx
  accepted, which no request header can influence — and from the GeoIP2 lookup
  on that same address. A visitor who sends their own `BZ-Connecting-IP` has it
  replaced; a header whose switch is off is *cleared* rather than passed
  through, so nothing in the `BZ-` namespace ever reaches an origin carrying
  client input. Incoming `X-Forwarded-For`, `X-Real-IP`, `True-Client-IP` and
  `CF-Connecting-IP` are never consulted as a source.
- **Origins should only trust them when origin access is restricted to trusted
  BlitzeCDN edges.** A `BZ-Connecting-IP` arriving on a connection that did not
  come from an edge means exactly as much as an `X-Forwarded-For` does —
  nothing. Firewall the origin to the edge addresses (`blitzecdn edge list`
  reports them) before reading these headers for rate limiting, geo-gating, or
  audit logs.
- **`BZ-IPCountry` requires GeoIP2/MaxMind support.** It reads the same
  `$blitzecdn_country` as the firewall's country rules, so it needs
  `blitzecdn_nginx_geoip_enabled: true` and a MaxMind country database on the
  edge. A site that asks for it without them **fails the deploy**, because an
  origin cannot tell a header that was never sent from a visitor whose country
  is unknown. Nothing is changed on the host when this fires.

`X-Real-IP` and `X-Forwarded-For` are unchanged and still sent. Neither header
affects the cache key: two visitors from different addresses or countries share
one cached object.

## Origin DNS

By default each edge re-resolves origin hostnames every
`blitzecdn_nginx_resolver_valid` (30s) through
`blitzecdn_nginx_resolvers` (the systemd-resolved stub, `127.0.0.53`), so an
origin that changes address is picked up without a reload. Set
`blitzecdn_nginx_resolvers: []` to pin origin addresses at config-load time
instead, which is nginx's default behaviour and appropriate when origins have
static addresses and you do not want a resolver in the request path.

## Backup and restore

One command group covers disaster recovery. `backup create` with no options
takes everything a rebuilt controller would otherwise have lost; `--only`
narrows it; `restore` puts back exactly what the archive says it holds.

```bash
blitzecdn backup create                      # full disaster-recovery backup
blitzecdn backup create --only database
blitzecdn backup create --only tls
blitzecdn backup create --only database --only tls
blitzecdn backup inspect FILE                # what an archive holds
blitzecdn backup restore FILE
```

Repeat `--only` to select exactly several components. Comma-separated values
are not accepted: use `--only database --only tls`, not
`--only database,tls`. Repeating the same component is also rejected.

```console
$ blitzecdn backup create
Backup created: /var/backups/blitzecdn/blitzecdn-backup-2026-08-30_00-15-30Z.tar.gz
```

### Components

| Component  | What it holds                                                                     |
| ---------- | --------------------------------------------------------------------------------- |
| `database` | Zones, records, edges, fleet-wide Ansible policy, deployment snapshots, audit log. |
| `tls`      | Installed certificate chains, their private keys, and the metadata pairing them.   |
| `acme`     | The local ACME account, its renewal configuration, and its certificate archive.    |
| `config`   | `blitzecdn.toml`, `.env`, and the controller SSH key pair every edge authorises.   |

A full backup takes every component that has data, so a controller that has
never issued a certificate still backs up cleanly. Naming a component that has
no data — `--only tls` on that same controller — is an error rather than an
empty archive.

Nothing derived is archived: no logs, no cache, no run or deployment working
directories, no generated `desired-state.yml`, no generated Nginx
configuration, no virtualenv, source, or packages. Edge configuration is
regenerated by the `deploy` that follows a restore.

Configuration is restored according to what it means, rather than copied over
the fresh installation indiscriminately:

- Portable authoritative state includes API and deployment secrets, ACME and
  operational policy, and other controller settings that remain meaningful on
  the replacement host. These values are restored.
- Machine-specific installation state includes the project, database, backup,
  and environment-file paths and the local Redis endpoint. The compatible
  fresh install's values are preserved; an archive never replaces them with
  paths or endpoints from the failed host.
- Regenerable state includes rendered desired state, run directories, logs,
  generated Nginx configuration, caches, and transient Redis contents. It is
  excluded and rebuilt from the restored database.

The controller SSH private and public keys are portable, non-regenerable
identity and are restored together. Replacing that pair would otherwise break
access to edges that already authorize the controller.

### Archives

One format for every backup — gzipped tar — so an archive can be read on a host
where BlitzeCDN will not start. Names are UTC and identical whatever the
archive holds, because the manifest is what differs:

```
blitzecdn-backup-2026-08-30_00-15-30Z.tar.gz
```

```
manifest.json
database/blitzecdn.db
tls/…
acme/…
config/…
```

`manifest.json` is the authoritative list of what is inside, and is what a
restore acts on:

```json
{
  "format_version": 1,
  "created_at": "2026-08-30T00:15:30Z",
  "blitzecdn_version": "2.6.1",
  "database_schema_version": "0001",
  "components": ["database", "tls"]
}
```

The backup format version and the database schema version are separate on
purpose: a migration changes what is inside `database/blitzecdn.db` without
changing the archive layout. A restore refuses a `format_version` newer than it
understands, an unknown component name, and a database schema revision this
installation has never heard of — it migrates forward and never downgrades.

Backups land in `backup_dir`, which the installed control plane sets to
`/var/backups/blitzecdn` (outside `/opt/blitzecdn`, so an uninstall cannot take
the backup with it) and a checkout defaults to `.state/backups`.

### Sensitivity

**An archive contains every TLS private key, the API credentials, the ACME
account key, and the controller's SSH key.** Archives are written `0600` into a
`0700` directory, are never silently overwritten, and are built beside their
destination and linked into place so a crash leaves a temporary file rather
than a truncated backup. There is no encryption: treat the archive itself as
the secret, and **copy it off the server** — a backup that only exists on the
host it protects is not a backup.

### Restoring onto a fresh install

```bash
# install a compatible BlitzeCDN release, then inspect before changing state:
blitzecdn backup inspect \
  /path/to/blitzecdn-backup-2026-08-30_00-15-30Z.tar.gz
blitzecdn backup restore \
  /path/to/blitzecdn-backup-2026-08-30_00-15-30Z.tar.gz
blitzecdn deploy
```

The whole archive is validated before anything is written: the member names,
the manifest, the format version, the component names, and each component's
required files. Only then are the control-plane services stopped — and only if
a component needs it, so a TLS-only restore never takes the controller offline
— the components restored, the database migrated, and the services started
again. A failure at any point leaves the services as it found them.

## Failure and recovery

- A filesystem lock permits one deployment at a time.
- Each run uses an immutable desired-state snapshot and bounded logs.
- Timeout and failed runs remain recorded and never become canonical rollbacks.
- Startup republishes queued records and marks orphaned running records
  `abandoned` only while holding the deployment lock.
- Rollback first converges the selected snapshot; canonical desired state changes
  only after Ansible succeeds.
- An invalid Nginx configuration fails `nginx -t` — in a throwaway container
  with the same mounts — before reload, leaving the running worker
  configuration active. Correct desired state and deploy again.
- An edge runtime image that fails validation or health is withdrawn and the
  edge returned to the exact digest it was running, and the run still fails.
- Ansible uses `serial: 25%` and `any_errors_fatal` to limit partial rollout,
  so a broken image stops at the first batch instead of reaching the fleet.
- The host keeps SSH, Docker, the firewall and kernel tuning natively, so an
  edge whose containers are all broken is still reachable and repairable.

SQLite and local locks support one control-plane node. Take a
`blitzecdn backup create` before controller maintenance, and rerun
`validate`/`plan` after a restore. For active/active operation, replace SQLite
and filesystem locking with transactional shared infrastructure.

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
