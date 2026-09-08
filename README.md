# BlitzeCDN

BlitzeCDN is a security-focused control plane for managing containerized Nginx
CDN edges with Python and Ansible.

Full configuration, operations, API, recovery, and architecture documentation
lives in [blitze-cdn-web](https://github.com/misaf/blitze-cdn-web). Use
`blitzecdn --help` or `blitzecdn COMMAND --help` for the current CLI reference.

## Requirements

- Python 3.12–3.14 for controller development
- A fresh Ubuntu 26.04 LTS server for a production standalone installation or
  edge
- Public-key SSH access to managed edges

## Standalone quick start

Install an independent control plane and edge on a fresh server:

```bash
sudo git clone --branch 4.x \
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
blitzecdn record add example.com cdn \
  --value origin.example.com --proxied
blitzecdn validate
blitzecdn plan
blitzecdn deploy
```

The API listens on loopback. Reach it without opening a public port:

```bash
ssh -L 8000:127.0.0.1:8000 OPERATOR@EDGE_ADDRESS
```

Swagger UI is then available at `http://127.0.0.1:8000/docs`.

To allow public API access from specific client IPs, set this in
`/etc/blitzecdn/blitzecdn.env` (replace the examples with your trusted addresses):

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
`BLITZE_ALLOWED_IPS` has its rule withdrawn rather than left behind. Rules you
added by hand are not touched, UFW is never enabled by this role, and the
default policy is left alone. Where UFW is inactive or another firewall is
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
`203.0.113.8/32` so the widening is chosen rather than assumed.

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

## Essential operations

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

On standalone installations, the host command uses the official Docker Python
SDK to run disposable application containers. It reads the managed service file
at `/etc/blitzecdn/control-plane.compose.yml`; installation and image builds still
use Compose. Restores stop the running API and worker, restore configuration and
credentials, then recreate those services and wait for them to become healthy.
The host Python environment is required for this command as well as installation.

Update an installed standalone server with:

```bash
sudo /opt/blitzecdn/install.sh update [--yes] [--no-backup]
```

There is no release to choose: the server moves to the newest `vMAJOR.MINOR.PATCH`
tag in its own major line, and tells you which two versions it is moving between
before it changes anything. It never crosses a major line:

```bash
sudo /opt/blitzecdn/install.sh upgrade [--yes] [--no-backup]
```

That is the deliberate step, one major line at a time, and it refuses a server
that has not finished its own line first — the releases you would be skipping
are where the removals were announced. It backs up everything rather than the
database alone, and asks you to type the version you are moving to.

## Extending BlitzeCDN

Capabilities register themselves through `pluggy`. Some are required parts of
the control plane; others are ordinary Python distributions that install beside
it and are found only through their entry points:

```bash
pip install blitzecdn                # the control plane alone
pip install 'blitzecdn[compression]' # + gzip and Brotli implementation
pip install 'blitzecdn[certificates]'# + certificate management and Automatic SSL
pip install 'blitzecdn[security]'    # + site security implementation
pip install 'blitzecdn[http3]'       # + HTTP/3 over QUIC (HTTP/1.1 and HTTP/2 are baseline)
pip install 'blitzecdn[geoip]'       # + visitor country lookup (headers and country rules)
pip install 'blitzecdn[hardening]'   # + public-key-only SSH and Fail2Ban on every edge host
pip install 'blitzecdn[resolver]'    # + host DNS resolution BlitzeCDN manages, off until enabled
pip install 'blitzecdn[all]'         # + every optional capability
pip uninstall blitzecdn-compression  # the capability disappears; core keeps working
```

A package can contribute routes, commands, scheduled jobs, health checks,
deployment checks and desired state without a line of this repository changing —
including one this repository has never heard of. See [PLUGINS.md](PLUGINS.md)
for how, and [COMPATIBILITY.md](COMPATIBILITY.md) for what a wheel may depend
on and what a version number obliges.

The stable `CdnSite` configuration remains part of core whether an implementation
wheel is installed or not. A site with compression off, unmanaged TLS, and the
default security and visitor-header settings works with core alone, and is
served over HTTP/1.1 and HTTP/2 — baseline protocol support needs no optional
wheel. Its per-site upload limit (`max_upload_size`, `100m` or `200m`) needs no
wheel either: `client_max_body_size` is always compiled into nginx. Requesting gzip/Brotli, uploaded/requested certificates, Automatic SSL
for active TLS, Under Attack Mode, site firewall rules, HTTP/3, the BZ-IPCountry
visitor header or country firewall rules without their provider fails
validation explicitly, naming the capability and the setting that asked for it;
it is never silently ignored or downgraded.

## Development

The repository is a [uv](https://docs.astral.sh/uv/) workspace: the root project
is `blitzecdn`, and each optional capability under `packages/` builds as its own
wheel. Tasks run through [just](https://just.systems/):

```bash
just install       # the whole workspace, including every optional capability
just check         # every CI gate, in CI order
```

Focused commands:

```bash
just test-package blitzecdn-cache   # one distribution's own tests
just test-core-only                 # the suite with no optional package installed
just build                          # every wheel and sdist
```

`just check` runs the same formatting, linting, type, test, Ansible, security,
build, and documentation checks used by CI. See `just --list` for focused
commands.

Report security issues privately to the maintainers. Do not publish credentials
or exploit details in an issue.
