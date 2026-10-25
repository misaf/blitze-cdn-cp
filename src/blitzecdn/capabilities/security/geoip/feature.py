"""Declare the application's GeoIP provider."""

from __future__ import annotations

from blitzecdn.capabilities.security.geoip import __version__, ansible
from blitzecdn.core.features import (
    AnsibleContribution,
    ConfigurationContribution,
    EdgeModule,
    EnvironmentKey,
    Feature,
    FeatureMetadata,
    NginxContribution,
)
from blitzecdn.core.runtime.resources import package_directory

#: The Jinja fragments this capability contributes to the edge's Nginx
#: configuration, resolved under the same guard its roles are. A sibling of
#: ``ansible/`` rather than a child: the controller renders these into each
#: release's configuration, so they are not part of any role this package
#: ships.
NGINX_TEMPLATES = (
    package_directory(
        __name__,
        resolves="Nginx templates are rendered from a filesystem path",
    )
    / "nginx"
)

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="geoip",
            version=__version__,
            provides=frozenset({"geoip"}),
            summary="Visitor IP-to-country lookup for country headers and rules.",
        ),
        # The MaxMind credentials the GeoLite2 updater authenticates with.
        # Deliberately not `required`: a controller with no MaxMind credentials
        # is a working controller, and a site that asks for a country is
        # refused by capability token rather than by a startup failure.
        configuration=(
            ConfigurationContribution(
                feature="geoip",
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
        ),
        ansible=(
            AnsibleContribution(
                feature="geoip",
                roles_path=ansible.ROLES_PATH,
                edge_roles=(ansible.EDGE_ROLE,),
                # No `probe`: every geoip2 directive takes a MaxMind database,
                # which `geoip2` opens while the configuration is parsed, and
                # the image has none. The module still has to load, which the
                # probe's `load_module` proves.
                edge_modules=(
                    EdgeModule(name="geoip2", objects=("ngx_http_geoip2_module.so",)),
                ),
            ),
        ),
        nginx=(
            NginxContribution(
                feature="geoip",
                templates_path=NGINX_TEMPLATES,
                http_fragments=("geoip-http.conf.j2",),
                upstream_fragments=("geoip-upstream.conf.j2",),
            ),
        ),
    ),
)
