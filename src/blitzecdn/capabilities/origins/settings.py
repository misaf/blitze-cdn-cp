"""Origin probe settings."""

from __future__ import annotations

from typing import Final

from pydantic import Field

from blitzecdn.core.features import ConfigurationModel

__all__ = ["CONFIGURATION", "OriginsConfig"]

CHECK_TIMEOUT: Final = "BLITZE_ORIGIN_CHECK_TIMEOUT_SECONDS"


class OriginsConfig(ConfigurationModel):
    """What an operator tunes about probing origins from the controller."""

    check_timeout_seconds: int = Field(
        default=5,
        ge=1,
        le=60,
        alias=CHECK_TIMEOUT,
        description=(
            "The controller's own advisory origin-probe budget in certificate "
            "preflight. The fleet check's is the origins role's default."
        ),
    )


CONFIGURATION = OriginsConfig.configuration("origins")
