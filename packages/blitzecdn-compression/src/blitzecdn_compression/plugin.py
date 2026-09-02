"""Register gzip and Brotli as one detachable compression capability."""

from collections.abc import Sequence
from pathlib import Path

from blitzecdn.core.plugins import (
    AnsibleContribution,
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
        ),
    )
