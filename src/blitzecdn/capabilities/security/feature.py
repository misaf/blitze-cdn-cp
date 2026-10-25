"""Declare the application's security capability."""

from __future__ import annotations

from blitzecdn.capabilities.security import ansible, cli
from blitzecdn.capabilities.security.settings import (
    MINIMUM_SECRET_BYTES,
    SECRET_VARIABLE,
)
from blitzecdn.core.features import (
    AnsibleContribution,
    CliCommandGroup,
    ConfigurationContribution,
    EdgeModule,
    EnvironmentKey,
    Feature,
    FeatureMetadata,
    NginxContribution,
)
from blitzecdn.core.runtime.resources import distribution_version, package_directory

#: The installed distribution's version, read from package metadata.
__version__ = distribution_version(__name__)

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
            name="security",
            version=__version__,
            provides=frozenset({"security"}),
            summary="Per-site request filtering and Under Attack Mode.",
        ),
        cli_commands=(
            CliCommandGroup(
                feature="security",
                name="domain",
                app=cli.domain_commands,
                extends=True,
            ),
        ),
        # The fleet key the Under Attack challenge is signed with. Optional: a site
        # that needs it is refused at deploy. The length is enforced at composition.
        configuration=(
            ConfigurationContribution(
                feature="security",
                environment_keys=(
                    EnvironmentKey(
                        name=SECRET_VARIABLE,
                        minimum_bytes=MINIMUM_SECRET_BYTES,
                        summary=(
                            "The fleet-wide key the Under Attack Mode challenge "
                            "is signed with."
                        ),
                    ),
                ),
            ),
        ),
        ansible=(
            AnsibleContribution(
                feature="security",
                roles_path=ansible.ROLES_PATH,
                edge_roles=(ansible.EDGE_ROLE,),
                # Under Attack Mode is njs. `build=False`: the official image ships it.
                edge_modules=(
                    EdgeModule(
                        name="njs",
                        objects=("ngx_http_js_module.so",),
                        build=False,
                        probe="js_path /etc/nginx;",
                    ),
                ),
            ),
        ),
        nginx=(
            NginxContribution(
                feature="security",
                templates_path=NGINX_TEMPLATES,
                http_fragments=("security-http.conf.j2",),
                server_fragments=("security-server.conf.j2",),
                access_fragments=("security-access.conf.j2",),
                upstream_fragments=("security-upstream.conf.j2",),
                # The Under Attack Mode implementation. Carries no secret, so
                # it ships in the configuration release with the fragments.
                module_fragments=("under-attack.js.j2",),
            ),
        ),
    ),
)
