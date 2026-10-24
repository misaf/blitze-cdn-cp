"""Register the application's geoip implementation.

All code and resources ship with BlitzeCDN. Registration contributes delivery,
configuration or concrete edge resources; business operations remain services.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn.core.plugins import (
    AnsibleContribution,
    ConfigurationContribution,
    EdgeModule,
    EnvironmentKey,
    NginxContribution,
    PluginMetadata,
    hookimpl,
)
from blitzecdn.core.runtime.resources import package_directory
from blitzecdn.providers.geoip import __version__, ansible

#: The Jinja fragments this capability contributes to the edge's Nginx
#: configuration, resolved under the same guard its roles are. A sibling of
#: ``ansible/`` rather than a child: core's ``blitzecdn_nginx`` renders these
#: from the resolved contribution, so they are not part of any role this
#: package ships.
NGINX_TEMPLATES = (
    package_directory(
        __name__,
        resolves="Nginx templates are rendered from a filesystem path",
    )
    / "nginx"
)


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="geoip",
        version=__version__,
        api_version=1,
        required=True,
        provides=frozenset({"geoip"}),
        summary="Visitor IP-to-country lookup for country headers and rules.",
    )


@hookimpl
def blitzecdn_capability_configuration() -> Sequence[ConfigurationContribution]:
    """Claim the MaxMind credentials the GeoLite2 updater authenticates with.

    Deliberately not `required`. A controller with no MaxMind credentials is a
    working controller: the role provisions no database,
    `blitzecdn_geoip_enabled` stays the fleet's answer, and a site that asks
    for a country is refused by capability token rather than by a startup
    failure nobody asked for.
    """
    return (
        ConfigurationContribution(
            plugin="geoip",
            environment_keys=(
                EnvironmentKey(
                    name="BLITZE_MAXMIND_ACCOUNT_ID",
                    summary=(
                        "The MaxMind account id the GeoLite2 updater "
                        "authenticates with."
                    ),
                ),
                EnvironmentKey(
                    name="BLITZE_MAXMIND_LICENSE_KEY",
                    summary=(
                        "The MaxMind license key the GeoLite2 updater "
                        "authenticates with."
                    ),
                ),
            ),
        ),
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
            # The module the resources below are written against.
            #
            # No `probe`, and that is not an omission: every geoip2 directive
            # takes a MaxMind database, which `geoip2` opens while the
            # configuration is parsed, and the image has none and must not
            # carry one. The module still has to load, which the probe's
            # `load_module` proves; only the directive check is unavailable.
            edge_modules=(
                EdgeModule(
                    name="geoip2",
                    objects=("ngx_http_geoip2_module.so",),
                ),
            ),
        ),
    )


@hookimpl
def blitzecdn_nginx_contributions() -> Sequence[NginxContribution]:
    return (
        NginxContribution(
            plugin="geoip",
            templates_path=NGINX_TEMPLATES,
            http_fragments=("geoip-http.conf.j2",),
            upstream_fragments=("geoip-upstream.conf.j2",),
        ),
    )
