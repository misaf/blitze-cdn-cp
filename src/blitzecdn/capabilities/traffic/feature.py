"""Traffic accounting and its recurring collection job."""

from blitzecdn.capabilities.traffic import ansible
from blitzecdn.capabilities.traffic.settings import CONFIGURATION
from blitzecdn.capabilities.traffic.wiring import __version__
from blitzecdn.core.features import AnsibleContribution, Feature, FeatureMetadata

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="traffic",
            version=__version__,
            provides=frozenset({"traffic"}),
            summary="Collect hourly visitor bandwidth and request totals.",
        ),
        configuration=(CONFIGURATION,),
        ansible=(
            AnsibleContribution(feature="traffic", roles_path=ansible.ROLES_PATH),
        ),
    ),
)
