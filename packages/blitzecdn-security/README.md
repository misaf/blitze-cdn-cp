# blitzecdn-security

Optional per-site request filtering and Under Attack Mode implementation for
BlitzeCDN. Install this package to make the `security` capability available.

The stable `SecurityPolicy` remains in the core package: `firewall` and
`under_attack_mode` are fields on the flat `CdnSite` that the v1/v2 HTTP
schemas, the persisted policy JSON and the versioned deployment snapshots all
consume, so the site schema is identical whether or not this package is
installed and a stored site that asks for the challenge still loads on a
controller that has detached it. A site with default or disabled security
configuration needs nothing from this wheel.

| This package | The site asks for Under Attack Mode | Result |
| --- | --- | --- |
| absent | no | Served normally. Nothing is missing and nothing is reported. |
| absent | yes | Deployment is refused at validation, naming the `security` capability. |
| installed, no secret | yes | Refused at `blitzecdn validate`, naming `BLITZE_UNDER_ATTACK_SECRET`, before a document is rendered or a play starts. |
| installed, secret set | yes | The edge serves a signed browser challenge and issues clearance cookies. |

A firewall *country* list needs two capabilities: `security` for the rule and
`geoip` for the lookup. They are reported separately, because detaching either
is a different problem, and this package never imports `blitzecdn_geoip` — it
depends on the token, which is what lets another distribution answer for
`geoip` one day.

Attach it in a checkout with `uv sync --extra security`, beside an installed
control plane with `pip install 'blitzecdn[security]'`, or in an installation
by adding `security` to `BLITZECDN_CAPABILITIES`.

## What ships in this wheel

```
src/blitzecdn_security/
├── plugin.py                         # metadata, deployment check, contribution
├── config.py                         # this capability's own settings
├── nginx/                             # HTTP/server/access/upstream resources
├── ansible/__init__.py               # where the role landed
└── ansible/roles/blitzecdn_security/
    ├── defaults/main.yml
    ├── tasks/main.yml
    ├── meta/argument_specs.yml
    └── templates/
        └── under-attack.js.j2        # the njs challenge implementation
```

Both halves of the refusal live here. The deployment check runs at `blitzecdn
validate`, before anything is written; the role's assertion runs on the edge,
for a desired state that did not come from this control plane. Neither replaces
the other.

## Configuration

The fleet challenge secret goes in the controller's 0600 `.env`:

```
BLITZE_UNDER_ATTACK_SECRET=<at least 32 bytes>
```

It is not a named field on core's `Settings`: this package explicitly claims
the key in its `AnsibleContribution`, and the control plane forwards only keys
claimed by one installed plugin. This package reads the same staged value from
`Settings.capability_environment` for its deployment check. It never enters
TOML, desired state, the fleet settings
database or an Ansible command line, and the njs module it is written into is
`root:www-data` 0640 with `no_log` on every task that touches it.

Keep it stable across the fleet, or a clearance stops being valid when a
visitor reaches a different edge inside its lifetime.

Whether the fleet can serve the challenge at all is fleet Ansible policy:

```bash
blitzecdn config set blitzecdn_security_under_attack_enabled true
```

## What this package does not own

The generic server, TLS, proxy and origin-routing skeleton. This package owns
the challenge locations and request-security directives it contributes at the
core renderer's stable server, access and upstream contexts.
The njs module those locations dispatch into lands in the runtime contract's
`paths.modules` directory, which core creates and mounts read-only.
