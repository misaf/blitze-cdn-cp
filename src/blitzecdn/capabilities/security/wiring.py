"""What the security capability checks before a release is rendered."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from blitzecdn.capabilities.security.contract import SecurityPolicy
from blitzecdn.capabilities.security.settings import SECRET_VARIABLE, SecurityConfig
from blitzecdn.core.features import FeatureRendering, Severity, ValidationIssue

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.core.domain.site import CdnSite


def security_rendering(*, config: SecurityConfig) -> FeatureRendering[CdnSite]:
    """Refuse an enabled site asking for Under Attack Mode when no secret is set."""

    def checks(site: CdnSite) -> Sequence[ValidationIssue]:
        if not site.policy.enabled or not site.policy[SecurityPolicy].under_attack_mode:
            return ()
        if config.challenge_available:
            return ()
        return (
            ValidationIssue(
                feature="security",
                site=site.name,
                message=(
                    f"under_attack_mode is on but {SECRET_VARIABLE} is not set on "
                    "this controller, so the edge challenge capability cannot be "
                    "enabled and the deployment would fail on every edge."
                ),
                severity=Severity.BLOCKING,
            ),
        )

    return FeatureRendering(feature="security", deployment_checks=checks)
