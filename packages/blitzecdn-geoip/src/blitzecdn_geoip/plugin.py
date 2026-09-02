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
* The *edge realization* ships in this wheel. The ``blitzecdn_geoip`` role
  beside this module provisions the GeoLite2 database, owns the MaxMind
  credential and the updater's Compose project, installs the systemd timer that
  refreshes it, while package-owned Nginx resources define and consume
  ``$blitzecdn_country``. Core's edge play and renderer discover those resources
  because this plugin says so,
  not because the play names it; detaching the distribution removes the role
  from Ansible's search path and from the play together.

  Core retains only the generic visitor-country variable/header contract and
  stable fragment insertion context. It contains no GeoIP2 implementation.

Whether an edge has the capability switched on is fleet Ansible policy
(``blitzecdn_geoip_enabled``, in this role's own defaults) and not desired
state, so this plugin contributes no desired-state variable and an installation
converges byte-identical documents whether or not it is attached. Attaching the
distribution makes country-aware sites deployable and brings the role that
serves them; detaching makes them refused before any playbook runs, with
nothing in core edited either way.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from blitzecdn.core.plugins import (
    AnsibleContribution,
    EdgeModule,
    NginxContribution,
    PluginMetadata,
    hookimpl,
)
from blitzecdn_geoip import __version__, ansible


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="geoip",
        version=__version__,
        required=False,
        provides=frozenset({"geoip"}),
        summary="Visitor IP-to-country lookup for country headers and rules.",
    )


@hookimpl
def blitzecdn_ansible_contributions() -> Sequence[AnsibleContribution]:
    # The role, and the fact that the edge play should run it. Core adds the
    # directory to Ansible's search path, adds the name to the play's
    # capability slot, and never learns what either contains.
    return (
        AnsibleContribution(
            plugin="geoip",
            roles_path=ansible.ROLES_PATH,
            edge_roles=(ansible.EDGE_ROLE,),
            # The module the resources below are written against. It used to
            # be built into the edge image and loaded unconditionally, so an
            # edge with this distribution detached still loaded GeoIP2 — the
            # image was a second, stale answer to which capabilities exist.
            #
            # No `probe`, and that is not an omission. The image's build-time
            # probe evaluates one directive per module to catch a module that
            # loads but registers nothing, and every geoip2 directive takes a
            # MaxMind database: `geoip2` opens the file while the configuration
            # is parsed, and the image has none and must not carry one. The
            # module still has to load, which the probe's `load_module` proves;
            # only the directive check is unavailable here.
            edge_modules=(
                EdgeModule(
                    name="geoip2",
                    objects=("ngx_http_geoip2_module.so",),
                ),
            ),
            environment_keys=(
                "BLITZE_MAXMIND_ACCOUNT_ID",
                "BLITZE_MAXMIND_LICENSE_KEY",
            ),
        ),
    )


@hookimpl
def blitzecdn_nginx_contributions() -> Sequence[NginxContribution]:
    return (
        NginxContribution(
            plugin="geoip",
            templates_path=Path(__file__).with_name("nginx"),
            http_fragments=("geoip-http.conf.j2",),
            upstream_fragments=("geoip-upstream.conf.j2",),
        ),
    )
