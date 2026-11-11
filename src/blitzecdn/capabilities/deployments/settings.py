"""Convergence settings: how long a run may take, and how much history to keep."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import Field

from blitzecdn.core.features import ConfigurationModel

__all__ = ["CONFIGURATION", "DeploymentsConfig", "lock_path"]

TIMEOUT: Final = "BLITZE_DEPLOYMENT_TIMEOUT_SECONDS"
RETENTION: Final = "BLITZE_DEPLOYMENT_RETENTION"
DRIFT_CHECK_INTERVAL: Final = "BLITZE_DRIFT_CHECK_INTERVAL_SECONDS"

#: The one lock every convergence and every material change serialises on,
#: beside the rest of the controller's state. Not configurable: two
#: controllers sharing a state directory must agree on it.
_LOCK_FILE: Final = "deployment.lock"


class DeploymentsConfig(ConfigurationModel):
    """What an operator tunes about convergence."""

    timeout_seconds: int = Field(
        default=900,
        ge=30,
        le=7200,
        alias=TIMEOUT,
        description=(
            "Wall-clock budget for one Ansible run, after which it is stopped. "
            "Bounds every fleet play, and ACME issuance."
        ),
    )
    retention: int = Field(
        default=100,
        ge=10,
        le=100_000,
        alias=RETENTION,
        description=(
            "Finished real deployments to keep, newest first, plus the newest "
            "successful full-fleet deployment of as many recently converged "
            "releases, which rollback chooses from."
        ),
    )
    drift_check_interval_seconds: int = Field(
        default=3600,
        ge=0,
        le=86_400,
        alias=DRIFT_CHECK_INTERVAL,
        description="How often to check the fleet for drift. 0 disables it.",
    )


def lock_path(state_dir: Path) -> Path:
    """Where the deployment lock lives under the controller's state directory."""
    return state_dir / _LOCK_FILE


CONFIGURATION = DeploymentsConfig.configuration("deployments")
