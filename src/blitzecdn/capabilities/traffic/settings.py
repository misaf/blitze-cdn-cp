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
    collection_max_bytes: int = Field(
        default=256 * 1024 * 1024,
        ge=65536,
        le=1024 * 1024 * 1024,
        alias="BLITZE_TRAFFIC_COLLECTION_MAX_BYTES",
        description=(
            "New log bytes each edge reads per collection pass. Size it from the "
            "edge's log rate; see docs/traffic-accounting.md."
        ),
    )
    collection_max_passes: int = Field(
        default=3,
        ge=1,
        le=10,
        alias="BLITZE_TRAFFIC_COLLECTION_MAX_PASSES",
        description="Maximum bounded log-collection passes in one scheduled run.",
    )
    top_paths: int = Field(
        default=20,
        ge=0,
        le=100,
        alias="BLITZE_TRAFFIC_TOP_PATHS",
        description=(
            "Busiest request paths kept per edge, hostname and hour for site "
            "analytics, without query strings. 0 stops collecting paths."
        ),
    )


CONFIGURATION = TrafficConfig.configuration("traffic")
