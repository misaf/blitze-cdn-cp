# blitzecdn-geoip

Optional visitor geographical-lookup implementation for BlitzeCDN. Install this
package to make the `geoip` capability available.

A CDN site does not need it. Serving a hostname, filtering by source address,
method or path, compression, TLS and every HTTP version work with nothing
installed beside the control plane. What needs `geoip` is a site that asks the
edge *which country a visitor is in*, and there are exactly three settings that
do:

| setting | what it asks for |
| --- | --- |
| `visitor_headers.ip_country` | the `BZ-IPCountry` request header written to the origin |
| `firewall.allowed_countries` | serve only these countries; everything else gets a 403 |
| `firewall.denied_countries` | answer these countries with a 403 |

All three stay in `blitzecdn`. They are fields on the flat `CdnSite` that the
v1/v2 HTTP schemas, the persisted policy JSON and the versioned deployment
snapshots consume, so the site schema is identical whether or not this package
is installed, and a stored site that asks for a country still loads on a
controller that has detached it.

| This package | The site asks for a country | Result |
| --- | --- | --- |
| absent | no | Served normally. Nothing is missing and nothing is reported. |
| absent | yes | Deployment is refused at validation, naming the `geoip` capability *and* the setting that requested it. No country header is silently omitted and no country rule is silently dropped. |
| installed | yes | The site deploys; the edge resolves the visitor address against its GeoIP2 database. |
| installed | no | Identical to the detached fleet document. Attaching converges nothing on its own. |

Attach it in a checkout with `uv sync --extra geoip`, beside an installed
control plane with `pip install 'blitzecdn[geoip]'`, or in an installation by
adding `geoip` to `BLITZECDN_CAPABILITIES`.

## What ships in this wheel

Everything that exists on an edge *because* this capability is attached:

```
src/blitzecdn_geoip/
├── plugin.py                      # metadata, and the Ansible contribution
├── nginx/
│   ├── geoip-http.conf.j2         # defines $blitzecdn_country
│   └── geoip-upstream.conf.j2     # assigns the generic country contract
├── ansible/__init__.py            # where the role landed, via importlib.resources
└── ansible/roles/blitzecdn_geoip/
    ├── defaults/main.yml          # fleet settings, credentials, paths, schedule
    ├── tasks/main.yml             # provision, assert, and withdraw
    ├── handlers/main.yml
    ├── meta/argument_specs.yml
    └── templates/
        ├── compose.yml.j2         # the updater's own Compose project
        ├── geoipupdate.env.j2     # the MaxMind credentials, 0600 root
        ├── geoipupdate.service.j2
        └── geoipupdate.timer.j2
```

The plugin answers `blitzecdn_ansible_contributions` with that roles directory
and the role name core's edge play should run. Installing the wheel makes both
appear; uninstalling makes them disappear, with no line of the control plane
edited either way.

## Configuration

Two settings, and neither is a field on core's `Settings`.

The credentials go in the controller's 0600 `.env`:

```
BLITZE_MAXMIND_ACCOUNT_ID=123456
BLITZE_MAXMIND_LICENSE_KEY=your-key
```

This package explicitly claims both keys in its `AnsibleContribution`. The
control plane forwards only keys claimed by one installed plugin, and the role
reads these with `lookup('env', ...)`
and the key never reaches the command line, the Compose file, `docker inspect`
or any file the control plane writes. GeoLite2 is free but the download is
authenticated; without credentials the capability still works, with the
operator placing a database at the configured path by hand.

Whether the fleet resolves countries at all is ordinary fleet Ansible policy:

```bash
blitzecdn config set blitzecdn_geoip_enabled true
```

Installing the distribution *provides* the capability; this switches it on. The
two are separate on purpose — a controller may carry the package for one fleet
and not use it on another — and the plugin contributes no desired state, so the
rendered document is byte-identical with and without it installed.

## What this package does not own

Two things stay in the control plane, and both are seams rather than
implementation:

* **the capability data directory.** `blitzecdn_edge_runtime.paths.data` is
  created by core and mounted read-only into the edge container and into the
  configuration-test container. This role puts its database under it; core
  never learns what is in there.
* **reading `$blitzecdn_country`.** The country rules and the `BZ-IPCountry`
  header are directives inside a server block, rendered by `blitzecdn_nginx`
  from desired state like every other site setting. Splitting a server block
  across packages would mean concatenating configuration text through a hook.
  The `geoip2` block that *defines* the variable is this package's, in its own
  `conf.d` snippet.

Also not here, deliberately: the database refresh is a provisioning lifecycle
rather than recurring control-plane behavior, so this package contributes no
scheduled job and the control plane performs no network download.
