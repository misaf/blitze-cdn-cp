"""Declare the application's security capability."""

from __future__ import annotations

from blitzecdn.capabilities.security import ansible
from blitzecdn.capabilities.security.config import (
    MINIMUM_SECRET_BYTES,
    SECRET_VARIABLE,
)
from blitzecdn.core.plugins import (
    AnsibleContribution,
    ConfigurationContribution,
    EdgeModule,
    EnvironmentKey,
    Feature,
    NginxContribution,
    PluginMetadata,
)
from blitzecdn.core.runtime.resources import distribution_version, package_directory

#: This distribution's version, asked of the environment rather than
#: written down here: it is what ``PluginMetadata.version`` reports and
#: what ``blitzecdn plugins`` shows an operator, so the one number that
#: must not drift from ``pyproject.toml`` is not copied out of it.
__version__ = distribution_version(__name__)

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

FEATURES = (
    Feature(
        metadata=PluginMetadata(
            name="security",
            version=__version__,
            api_version=2,
            required=True,
            provides=frozenset({"security"}),
            summary="Per-site request filtering and Under Attack Mode.",
        ),
        # The fleet-wide key the Under Attack Mode challenge is signed with.
        # Not `required`: a controller with no signing secret is a working
        # control plane with one site setting it will refuse, which is a
        # deployment check and not a startup failure. The length is declared
        # so a placeholder is refused at composition.
        configuration=(
            ConfigurationContribution(
                plugin="security",
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
                plugin="security",
                roles_path=ansible.ROLES_PATH,
                edge_roles=(ansible.EDGE_ROLE,),
                # Under Attack Mode is an njs script, so this capability needs
                # the JavaScript engine. `build=False`: njs ships in the
                # official Alpine image already, and asking pkg-oss to build a
                # module the base has installed fails the image build.
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
                plugin="security",
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
