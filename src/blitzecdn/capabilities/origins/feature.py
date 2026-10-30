"""Declare origin identity policy and origin probing."""

from __future__ import annotations

from blitzecdn.capabilities.origins import __version__, ansible, cli
from blitzecdn.capabilities.origins.api import routes
from blitzecdn.capabilities.origins.contract import OriginPolicy
from blitzecdn.core.features import (
    AnsibleContribution,
    CliCommandGroup,
    Feature,
    FeatureMetadata,
    PolicyContribution,
)

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="origins",
            version=__version__,
            provides=frozenset({"origins"}),
            summary="Origin request identity and probes from the proxying edges.",
        ),
        api_routers=(routes.router,),
        cli_commands=(
            CliCommandGroup(feature="origins", name="origin", app=cli.origin_app),
        ),
        site_policy=(
            PolicyContribution(
                feature="origins", group="origin", contract=OriginPolicy
            ),
        ),
        # The probe role, and neither slot of the edge play: this package's own
        # play reaches it, and it converges nothing on a deploy.
        ansible=(
            AnsibleContribution(feature="origins", roles_path=ansible.ROLES_PATH),
        ),
    ),
)
