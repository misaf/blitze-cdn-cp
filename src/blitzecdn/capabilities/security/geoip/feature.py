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

#: The Nginx fragments this capability contributes, rendered into each
#: release on the controller rather than shipped inside a role.
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
        # MaxMind credentials for the GeoLite2 updater; optional, since sites needing
        # geoip are refused by capability token.
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
                # No `probe`: every geoip2 directive needs a database the image lacks;
                # the `load_module` alone proves it loads.
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
