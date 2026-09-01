"""Register the capability that resolves a visitor address to a country.

One capability, two consumers. ``BZ-IPCountry`` is a visitor header the site
contract owns and the country firewall lists belong to ``SecurityPolicy``, but
both ask the same question of the edge — *which country is this address in* —
and both are answered by the same GeoIP2 database and the same Nginx module.
So there is one distribution rather than one per consumer, and a third consumer
(analytics, geographical routing) attaches to this token rather than adding
another wheel.

What this package owns is whether the control plane offers the capability at
all. That is deliberately the whole of it:

* The *contracts* stay in core. ``visitor_headers.ip_country``,
  ``allowed_countries`` and ``denied_countries`` are fields on the flat
  ``CdnSite`` that the v1/v2 schemas, the persisted policy JSON and the
  deployment snapshots all consume, so a controller without this package still
  reads back a site that asks for a country and then refuses to deploy it by
  name. A field that travelled with the wheel would make a stored row
  unreadable on detachment.
* The *derivation* stays in core too. ``SitePolicy.capability_requirements``
  maps the settings above onto this token the same way it maps `compression`
  or `http3_enabled` onto theirs — one generic mechanism, no per-package
  branch in any service.
* The *edge realization* stays in Ansible. The MaxMind database, its scheduled
  refresh, the ``geoipupdate`` unit and its credentials, the mount into the
  Nginx container, the ``geoip2`` directive and the module probe are all in the
  roles, which remain the provisioning authority. Whether an edge has GeoIP
  switched on is fleet Ansible policy (``blitzecdn_edge_geoip_enabled``), not
  desired state, so this plugin contributes no variable and an installation
  converges byte-identical documents whether or not it is attached.

That leaves metadata, and metadata is enough: attaching the distribution makes
country-aware sites deployable, and detaching it makes them refused before any
playbook runs, with nothing in core edited either way.
"""

from __future__ import annotations

from blitzecdn.core.plugins import PluginMetadata, hookimpl
from blitzecdn_geoip import __version__


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="geoip",
        version=__version__,
        required=False,
        provides=frozenset({"geoip"}),
        summary="Visitor IP-to-country lookup for country headers and rules.",
    )
