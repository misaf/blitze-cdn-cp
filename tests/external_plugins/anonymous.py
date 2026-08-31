"""An external plugin that contributes without saying who it is."""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn.core.plugins import HealthCheck, hookimpl


@hookimpl
def blitzecdn_health_checks(platform: object) -> Sequence[HealthCheck]:
    return (HealthCheck(name="anonymous", check=lambda: None),)
