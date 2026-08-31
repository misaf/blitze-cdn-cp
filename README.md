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
sudo git clone --branch 3.x \
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

Update an installed standalone server with:

```bash
sudo /opt/blitzecdn/install.sh update [--ref REF] [--yes]
```

Run `install.sh update --help` before changing release lines.

## Extending BlitzeCDN

Capabilities register themselves through `pluggy`. Some are required parts of
the control plane; others are ordinary Python distributions that install beside
it and are found only through their entry points:

```bash
pip install blitzecdn                # the control plane alone
pip install blitzecdn-cache          # + purge and cache reporting
pip install 'blitzecdn[all]'         # + every optional capability
pip uninstall blitzecdn-cache        # the capability disappears; core keeps working
```

A package can contribute routes, commands, scheduled jobs, health checks,
deployment checks and desired state without a line of this repository changing —
including one this repository has never heard of. See [PLUGINS.md](PLUGINS.md).

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
