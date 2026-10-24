"""Register the application's origins implementation.

All code and resources ship with BlitzeCDN. Registration contributes delivery,
configuration or concrete edge resources; business operations remain services.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn.capabilities.origins import __version__, ansible, cli
from blitzecdn.capabilities.origins.api import routes
from blitzecdn.core.plugins import (
    AnsibleContribution,
    CliCommandGroup,
    PluginMetadata,
    hookimpl,
)


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="origins",
        version=__version__,
        api_version=1,
        required=True,
        provides=frozenset({"origins"}),
        summary="Probe every site's origin from the edges that proxy to it.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (routes.router,)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    # `origin check`, exactly as it read before this became a package. The
    # command tree is an interface decision and not a consequence of
    # packaging.
    return (CliCommandGroup(plugin="origins", name="origin", app=cli.origin_app),)


@hookimpl
def blitzecdn_ansible_contributions() -> Sequence[AnsibleContribution]:
    # The probe role, from inside this wheel, and neither slot of the edge
    # play: this role is reached by this package's own play and converges
    # nothing on a deploy. Core adds the directory to Ansible's role search
    # path and never learns what is in it.
    return (AnsibleContribution(plugin="origins", roles_path=ansible.ROLES_PATH),)
