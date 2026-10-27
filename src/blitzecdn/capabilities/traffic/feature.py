"""Traffic accounting infrastructure; scheduling and delivery are separate."""

from blitzecdn.capabilities.traffic import ansible
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
        ansible=(
            AnsibleContribution(feature="traffic", roles_path=ansible.ROLES_PATH),
        ),
    ),
)
