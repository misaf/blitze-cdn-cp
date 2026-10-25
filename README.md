# BlitzeCDN

BlitzeCDN is a security-focused control plane for managing containerized Nginx
CDN edges with Python and Ansible.

Full configuration, operations, API, recovery, and architecture documentation
lives in [blitze-cdn-web](https://github.com/misaf/blitze-cdn-web). Use
`blitzecdn --help` or `blitzecdn COMMAND --help` for the current CLI reference.

Implementation decisions are recorded in [docs/decisions](docs/decisions/0001-zone-policy-and-composition.md).

## Requirements

- Python 3.12–3.14 for controller development
- A fresh Ubuntu 26.04 LTS server for a production standalone installation or
  edge
- Public-key SSH access to managed edges

## Local database lifecycle

After installing the checkout, run `blitzecdn setup` to initialize its database
and create local API credentials. Ordinary commands, the API and the worker
require an already prepared schema and never migrate it at startup.

For maintenance, stop controller services, preserve a backup and run
`blitzecdn migrate` before restarting them. This command changes only the schema;
it does not scaffold credentials. The managed installer performs this ordering
before starting its Compose services. Restore validates and migrates its staged
database before publishing it.

## Standalone quick start

Install an independent control plane and edge on a fresh server:

```bash
sudo git clone --branch 5.x \
  https://github.com/misaf/blitze-cdn-cp.git /opt/blitzecdn
sudo /opt/blitzecdn/install.sh standalone \
  --admin-cidr 203.0.113.8/32 \
  --public-address 203.0.113.10 \
  --email admin@example.com
```

Replace the example addresses. `--admin-cidr` is the network allowed to
administer the server over SSH; `--public-address` is the public A/AAAA address
for the edge.

Create the desired site, review the plan, and deploy it:

```bash
blitzecdn domain add example.com
blitzecdn record add example.com cdn --value 198.51.100.10
blitzecdn validate
blitzecdn plan
blitzecdn deploy
```

Replace `198.51.100.10` with the origin server's IPv4 address. It is the
server the edge fetches from, distinct from the edge's public address. These
examples serve HTTP; configure an active certificate before enabling visitor TLS.
The zone holds the policy, while the record supplies the hostname and origin
address. Records are proxied by default. To take one back off the edge, name the
address DNS should answer with instead:

```bash
blitzecdn record unproxy example.com cdn --value 203.0.113.9
```

### BlitzeCDN does not publish DNS

It records zones and records; it operates no authoritative nameserver and talks
to no DNS provider. A record written here is desired state, and a change reaches
no visitor until whatever is authoritative for the zone is told about it — so
`blitzecdn record add`, `record proxy` and `record unproxy` each do one half of
the job and say which half.

Export the other half for whatever does publish:

```bash
blitzecdn dns export
```

Each record says what DNS should answer with: a proxied hostname needs an edge
address, which the fleet supplies, and an unproxied one needs the record's own
value. `blitzecdn doctor` reports the same limitation as `dns_publication`.

The API listens on loopback. Reach it without opening a public port:

```bash
ssh -L 8000:127.0.0.1:8000 OPERATOR@EDGE_ADDRESS
```

Swagger UI is then available at `http://127.0.0.1:8000/docs`.

To allow public API access from specific client IPs, name them when you
install (comma-separated, or repeat the flag):

```bash
sudo /opt/blitzecdn/install.sh standalone \
  --admin-cidr 203.0.113.8/32 \
  --public-address 203.0.113.10 \
  --email admin@example.com \
  --allowed-ips 203.0.113.8/32,198.51.100.0/24
```

The API is then reachable from those addresses on its first boot, with UFW
already admitting exactly them — there is no window where one half of that
agreement is in force without the other.

`update` and `upgrade` take the same flag, so a release move can repoint the
list in one step:

```bash
sudo /opt/blitzecdn/install.sh update --allowed-ips 198.51.100.0/24
```

Leaving the flag off keeps whatever the server already has, hand edits
included, so a routine update never changes who may reach the API.

`blitzecdn doctor` reports the effective list as `api_allowed_ips`; an empty
one is a loopback-only listener.

To change the list without an installer run, edit
`/etc/blitzecdn/blitzecdn.env` (replace the examples with your trusted
addresses):

```dotenv
BLITZE_ALLOWED_IPS=203.0.113.8/32,198.51.100.0/24
```

Recreate the API to load the new environment:

```bash
sudo docker compose --file /etc/blitzecdn/control-plane.compose.yml \
  up -d --no-deps --force-recreate blitzecdn-api
```

The installed API binds to `0.0.0.0:8000` only when this list is non-empty.
Unlisted clients receive HTTP 403 on every route, including `/docs`,
`/openapi.json`, and `/health`. Loopback clients remain allowed for health
checks and SSH tunnels. Forwarded-IP headers are ignored; do not put an
unrestricted local reverse proxy in front of this listener, because it would
appear as loopback.

The list decides who may reach the API, not what they may do. Every control
endpoint still requires `X-API-Key`, and an admitted client that does not send
one gets HTTP 401. What being on the list grants without a key is the published
schema — `/docs`, `/redoc`, `/openapi.json` — and `/health`, so Swagger UI
works in a browser.

Where UFW is active, the installer converges port 8000 for exactly these
addresses on the next run:

```bash
sudo /opt/blitzecdn/install.sh update
```

It records the rules it installs, so an address removed from
`BLITZE_ALLOWED_IPS` has its rule withdrawn rather than left behind, and
`install.sh uninstall` withdraws all of them on the way out. Rules you added by
hand are not touched, UFW is never enabled by this role, and the default policy
is left alone. Where UFW is inactive or another firewall is
authoritative, the run says so and admits each source yourself:

```bash
sudo ufw allow from 203.0.113.8/32 to any port 8000 proto tcp
```

Apply the same source restrictions in your provider firewall if present. Until
the firewall agrees, an allowed address is denied at the packet rather than by
the API. You can then fetch `http://SERVER_IP:8000/openapi.json` from an
allowed IP. Direct port 8000 uses HTTP; use an SSH tunnel for encrypted access
when sending API credentials.

Set `BLITZE_ALLOWED_IPS=` and recreate the API to return to loopback-only access.
The environment file survives installer updates and backup/restore. For a
source checkout, the equivalent setting is `allowed_ips = ["203.0.113.8/32"]`
under `[blitzecdn]` in `blitzecdn.toml`; the environment takes precedence. Run
`python -m blitzecdn.api` to use automatic binding. The listener is IPv4, so
addresses are IPv4 too; an IPv6 entry is refused when the setting is read
rather than accepted and then never matched. A CIDR must have its host bits
clear: `203.0.113.8/24` is refused, naming both `203.0.113.0/24` and
`203.0.113.8/32` so the widening is chosen rather than assumed. `--allowed-ips`
applies the same three rules before the run changes anything, so a typo costs a
rejected command rather than a provisioned server or a stopped one.

## Controller quick start

For a controller-only checkout or development environment:

```bash
git clone https://github.com/misaf/blitze-cdn-cp.git
cd blitze-cdn-cp
BLITZECDN_DEV=1 ./install.sh
```

Register a fresh edge and deploy:

```bash
blitzecdn edge add edge-01 \
  --host 192.0.2.10 \
  --public-address 203.0.113.10 \
  --user deploy \
  --ssh-source 198.51.100.0/24
blitzecdn validate
blitzecdn plan
blitzecdn deploy
```

Verify SSH fingerprints through a trusted channel and use an SSH agent or a key
outside this repository. Do not disable host-key checking.

## Zone policy and rules

A zone carries the policy every hostname in it is served by — TLS, caching,
compression, the firewall, the headers sent to the origin. There is no second
object to create: `blitzecdn domain --help` lists the settings, and each is one
command.

```bash
blitzecdn domain add example.com
blitzecdn record add example.com cdn --value 198.51.100.10
blitzecdn domain show example.com
blitzecdn domain ssl example.com --mode off
blitzecdn domain cache example.com --success 1h
```

Creating a zone does not ask for an origin. Each proxied record's `--value`
is its origin address; the default record type is A and requires an IPv4 address.
Use `--type AAAA` with an IPv6 origin. The HTTP example above selects TLS Off;
Full (strict) requires active certificate material and a verified HTTPS origin.

When one hostname has to differ from the rest of its zone, write a rule rather
than a second configuration. A rule is a match and the settings it overrides,
and nothing else:

```bash
blitzecdn rule add example.com api \
  --match api.example.com --priority 10 --set cache_enabled=false
blitzecdn rule add example.com everything-else \
  --match '*' --priority 50 --set cache_valid_success=1h
blitzecdn rule list example.com
```

`--match` takes `*` for the whole zone, an exact hostname, or `*.suffix`.
Rules are ordered by `--priority`, lowest first, and **the first match wins**:
the winning rule's overrides are applied to the zone's policy and no other rule
contributes. `api.example.com` above is served with caching off and the zone's
own `cache_valid_success`, not the `1h` from the rule behind it.

To see what a hostname actually resolves to, and which rule decided it:

```bash
curl -sG http://127.0.0.1:8000/v1/domains/example.com/resolve \
  --data-urlencode hostname=api.example.com \
  -H "x-api-key: $BLITZE_API_KEY"
```

The response carries both the merged policy and the name of the rule that
applied, so "why is this hostname not caching" does not need a second request.
A rule whose overrides would produce an impossible zone — HTTP/3 on a hostname
serving no TLS — is refused where the zone itself would refuse it.

What an edge is actually asked to serve is derived from the three: one virtual
host per group of hostnames that resolve alike, named after the zone and the
rule that claimed them.

```bash
blitzecdn domain hosts
```

Nothing authors those. `example-com` is the zone's own policy and
`example-com--api` is the `api` rule's; to change either, change the zone, the
rule, or the records behind it.

## Essential operations

Start with the [short operating and recovery guide](docs/OPERATING_GUIDE.md) for
routine checks, interrupted work, rollback and restoring a controller. It states
which procedures were demonstrated and which real-edge checks remain deferred.

```bash
blitzecdn doctor
blitzecdn status
blitzecdn audit
blitzecdn backup create
blitzecdn backup inspect /path/to/backup.tar.gz
blitzecdn backup restore /path/to/backup.tar.gz
```

Backups contain credentials and private keys. Copy them off the server and
protect them as secrets.

See [retention and recovery copies](docs/RETENTION.md) for what cleanup keeps,
which keys rollback needs, and how to budget space for full backups. Archives
and real deployment history are retained until an operator decides otherwise.

### When a restarted worker still shows a running job

Inspect the job before submitting the same work again:

```bash
blitzecdn job list --status running --json
blitzecdn job show JOB_ID --json
```

Replace `JOB_ID` with the job identifier from the list. `leased_by` identifies
its recorded worker host/process; it does not prove that process is still alive.
`leased_until` is the ownership deadline, including its timezone. A restarted
worker cannot reclaim a dead worker's job before that deadline. Deployment jobs
use a 900-second (15-minute) lease by default; scheduled jobs may use other leases.
Read the recorded deadline rather than assuming a new 15-minute wait from restart.

If the recorded worker is still running, inspect that process and let its work
finish. If it has stopped and the deadline is in the future, wait for expiry.
Once eligible, a healthy polling worker can reclaim the job on a later poll;
lock contention, older runnable jobs and attempt limits can also delay or prevent
execution. An expired deadline alone does not prove successful recovery.

Inspect the job again after expiry. If no progress occurs, check the worker's
logs and health, the deployment lock and `last_error`. For deployment jobs,
`payload.deployment_id` identifies the deployment to inspect with
`blitzecdn status DEPLOYMENT_ID --json`. A failed job has reached a terminal
outcome and needs investigation. Do not edit queue rows, delete lock files or
submit a duplicate deployment merely to bypass lease waiting.

### Installed command and restore lifecycle

On standalone installations, the host command uses the official Docker Python
SDK to run disposable application containers. It reads the managed service file
at `/etc/blitzecdn/control-plane.compose.yml`; installation and image builds still
use Compose. Restores stop the running API and worker, restore configuration and
credentials, then recreate those services and wait for them to become healthy.
If a restore or configuration publication fails, the previous components are
recovered before services restart. If that recovery fails, services remain
stopped and the checkpoint is retained; resolve the reported problem and run
`blitzecdn backup recover` through the host wrapper.
The host Python environment is required for this command as well as installation.

Update an installed standalone server with:

```bash
sudo /opt/blitzecdn/install.sh update [--yes] [--no-backup] [--allowed-ips LIST]
```

There is no release to choose: the server moves to the newest `vMAJOR.MINOR.PATCH`
tag in its own major line, and tells you which two versions it is moving between
before it changes anything. It never crosses a major line:

```bash
sudo /opt/blitzecdn/install.sh upgrade [--yes] [--no-backup] [--allowed-ips LIST]
```

That is the deliberate step, one major line at a time, and it refuses a server
that has not finished its own line first — the releases you would be skipping
are where the removals were announced. It backs up everything rather than the
database alone, and asks you to type the version you are moving to.

## Application architecture and extensions

`blitzecdn` is one application distribution. It includes cache, compression,
TLS, security and origin operations, together with HTTP/3, GeoIP, hardening and
resolver providers. Site policy and provider configuration determine which
features run; there are no capability extras or sibling wheels to install.

Serving owns immutable site policy and effective virtual hosts. DNS owns zones,
rules, records and host derivation. Releases capture canonical inputs and
immutable artifacts; deployment verifies the release served by each edge.
Services receive focused ports, while the application composition root chooses
the adapters and owns their lifetime.

The application registers its mandatory built-ins explicitly in one roster;
there is no third-party plugin mechanism. See [PLUGINS.md](PLUGINS.md) for
ownership and assembly conventions, and [COMPATIBILITY.md](COMPATIBILITY.md) for
published contracts.

## Development

The repository builds one wheel and source archive from `src/blitzecdn/`.
Dependencies are locked with [uv](https://docs.astral.sh/uv/); tasks run through
[just](https://just.systems/):

```bash
just install                         # application and development tools
just check-quick                     # formatting, types, shell, Ansible and fast tests
just test-one tests/platform/test_repository.py
just test-installation               # install the actual wheel outside the checkout
just test                            # full suite with coverage and wheel installation
just build                           # one wheel and source archive
just check                           # all CI gates before pushing
```

Implementation decisions and verification status are recorded in
[docs/REIMPLEMENTATION.md](docs/REIMPLEMENTATION.md). Ubuntu deployment and
recovery verification is deferred; local tests do not establish live-edge
readiness. The [recovery acceptance checklist](docs/RECOVERY_ACCEPTANCE.md)
separates existing local evidence from recovery practice still to be performed.
See `just --list` for other focused commands.

Report security issues privately to the maintainers. Do not publish credentials
or exploit details in an issue.
