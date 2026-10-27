"""Traffic collection cadence and bounded catch-up work."""

from pydantic import Field

from blitzecdn.core.features import ConfigurationModel


class TrafficConfig(ConfigurationModel):
    retention_days: int = Field(
        default=90,
        ge=0,
        le=3650,
        alias="BLITZE_TRAFFIC_RETENTION_DAYS",
        description="Days of hourly traffic history to retain. 0 disables expiry.",
    )
    collection_interval_seconds: int = Field(
        default=60,
        ge=0,
        le=86400,
        alias="BLITZE_TRAFFIC_COLLECTION_INTERVAL_SECONDS",
        description=(
            "Seconds between visitor traffic collections. 0 disables scheduling."
        ),
    )
    collection_max_passes: int = Field(
        default=3,
        ge=1,
        le=10,
        alias="BLITZE_TRAFFIC_COLLECTION_MAX_PASSES",
        description="Maximum bounded log-collection passes in one scheduled run.",
    )


CONFIGURATION = TrafficConfig.configuration("traffic")
