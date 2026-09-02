"""Register gzip and Brotli as one detachable compression capability."""

from collections.abc import Sequence
from pathlib import Path

from blitzecdn.core.plugins import (
    AnsibleContribution,
    EdgeModule,
    NginxContribution,
    PluginMetadata,
    hookimpl,
)
from blitzecdn_compression import ansible

__version__ = "3.0.0"


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
            templates_path=Path(__file__).with_name("nginx"),
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
