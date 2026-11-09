"""Automatic SSL/TLS settings."""

from __future__ import annotations

from typing import Final

from pydantic import Field

from blitzecdn.core.features import ConfigurationModel

__all__ = ["CONFIGURATION", "AutomaticSslConfig"]

SCAN_INTERVAL: Final = "BLITZE_SSL_AUTOMATIC_SCAN_INTERVAL_SECONDS"


class AutomaticSslConfig(ConfigurationModel):
    """How often the fleet rescans origins for a stronger SSL mode."""

    scan_interval_seconds: int = Field(
        default=2_592_000,
        ge=0,
        le=31_536_000,
        alias=SCAN_INTERVAL,
        description="How often Automatic SSL/TLS rescans origins. 0 disables it.",
    )


CONFIGURATION = AutomaticSslConfig.configuration("automatic_ssl")
