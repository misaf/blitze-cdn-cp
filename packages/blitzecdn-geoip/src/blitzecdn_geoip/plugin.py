"""Register the capability that resolves a visitor address to a country.

One capability, two consumers. ``BZ-IPCountry`` is a visitor header the site
contract owns and the country firewall lists belong to ``SecurityPolicy``, but
both ask the same question of the edge — *which country is this address in* —
and both are answered by the same GeoIP2 database and the same Nginx module.
So there is one distribution rather than one per consumer, and a third consumer
attaches to this token rather than adding another wheel.

The contracts and capability requirements stay in core so stored site policy
remains readable when this wheel is detached. The edge realization ships here:
the role provisions the database and updater and defines the Nginx variable the
stable site renderer reads.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn.core.plugins import AnsibleContribution, PluginMetadata, hookimpl
from blitzecdn_geoip import __version__, ansible


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="geoip",
        version=__version__,
        summary="Visitor IP-to-country lookup for country headers and rules.",
    )


@hookimpl
def blitzecdn_ansible_contributions() -> Sequence[AnsibleContribution]:
    return (
        AnsibleContribution(
            plugin="geoip",
            roles_path=ansible.ROLES_PATH,
            edge_roles=(ansible.EDGE_ROLE,),
        ),
    )
