"""Declare the application's origin-probing capability."""

from __future__ import annotations

from blitzecdn.capabilities.origins import __version__, ansible, cli
from blitzecdn.capabilities.origins.api import routes
from blitzecdn.core.plugins import (
    AnsibleContribution,
    CliCommandGroup,
    Feature,
    PluginMetadata,
)

FEATURES = (
    Feature(
        metadata=PluginMetadata(
            name="origins",
            version=__version__,
            provides=frozenset({"origins"}),
            summary="Probe every site's origin from the edges that proxy to it.",
        ),
        api_routers=(routes.router,),
        cli_commands=(
            CliCommandGroup(plugin="origins", name="origin", app=cli.origin_app),
        ),
        # The probe role, and neither slot of the edge play: this package's own
        # play reaches it, and it converges nothing on a deploy.
        ansible=(AnsibleContribution(plugin="origins", roles_path=ansible.ROLES_PATH),),
    ),
)
