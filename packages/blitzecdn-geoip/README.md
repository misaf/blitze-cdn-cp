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

## What this package does not own

The Ansible roles remain the provisioning authority for how an edge realizes a
country lookup, and none of it moved into this wheel:

* the MaxMind account credentials and the `geoipupdate` container image;
* installing the GeoLite2-Country database and the systemd timer that refreshes
  it — a provisioning lifecycle, not recurring control-plane behavior, so this
  package contributes no scheduled job and performs no network download;
* mounting the database directory into the Nginx container, the `geoip2`
  directive and its `auto_reload`, and the `ngx_http_geoip2` module probe;
* `blitzecdn_edge_geoip_enabled`, which is *fleet* Ansible policy set with
  `blitzecdn config set` or in group vars.

That last one is why this plugin contributes no desired state. Whether an edge
has GeoIP switched on is a property of the fleet an operator configures, not a
variable the control plane derives per deployment, and turning it into one
would silently override what an operator set in group vars. The desired-state
document is therefore byte-identical with and without this package installed.
