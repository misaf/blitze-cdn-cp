"""Traffic accounting and its recurring collection job."""

from blitzecdn.capabilities.traffic import ansible, cli
from blitzecdn.capabilities.traffic.api import routes
from blitzecdn.capabilities.traffic.settings import CONFIGURATION
from blitzecdn.capabilities.traffic.wiring import __version__
from blitzecdn.core.features import (
    AnsibleContribution,
    CliCommandGroup,
    Feature,
    FeatureMetadata,
)

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="traffic",
            version=__version__,
            provides=frozenset({"traffic"}),
            summary="Collect hourly visitor bandwidth and request totals.",
        ),
        api_routers=(routes.router,),
        cli_commands=(
            CliCommandGroup(feature="traffic", name="traffic", app=cli.traffic_app),
        ),
        configuration=(CONFIGURATION,),
        ansible=(
            AnsibleContribution(feature="traffic", roles_path=ansible.ROLES_PATH),
        ),
    ),
)
