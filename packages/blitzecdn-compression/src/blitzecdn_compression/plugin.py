"""Register gzip and Brotli as one detachable compression capability."""

from collections.abc import Sequence

from blitzecdn.core.plugins import (
    AnsibleContribution,
    EdgeModule,
    NginxContribution,
    PluginMetadata,
    hookimpl,
)
from blitzecdn.core.runtime.resources import distribution_version, package_directory
from blitzecdn_compression import ansible

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


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="compression",
        version=__version__,
        required=False,
        provides=frozenset({"compression"}),
        summary="Which encodings a managed edge may produce: gzip and Brotli.",
    )


@hookimpl
def blitzecdn_nginx_contributions() -> Sequence[NginxContribution]:
    return (
        NginxContribution(
            plugin="compression",
            templates_path=NGINX_TEMPLATES,
            server_fragments=("compression-server.conf.j2",),
        ),
    )


@hookimpl
def blitzecdn_ansible_contributions() -> Sequence[AnsibleContribution]:
    return (
        AnsibleContribution(
            plugin="compression",
            roles_path=ansible.ROLES_PATH,
            edge_roles=(ansible.EDGE_ROLE,),
            # gzip is compiled into Nginx; Brotli is not, and it is the whole
            # reason this capability needs anything from the image. Only the
            # filter module is loaded: the static one serves pre-compressed
            # `.br` files from disk, which a proxy cache never has.
            edge_modules=(
                EdgeModule(
                    name="brotli",
                    objects=("ngx_http_brotli_filter_module.so",),
                    probe="brotli off;",
                ),
            ),
        ),
    )
