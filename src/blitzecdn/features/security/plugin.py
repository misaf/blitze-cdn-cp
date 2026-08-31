"""Register the security capability and what it refuses to deploy.

The deployment check is this capability's half of a control the edge role also
holds. ``blitzecdn_nginx`` asserts that no edge enables the challenge without a
secret of its own, and that assertion is the last line — it fires after the
desired-state document is written and a play has started. The same question
answered here costs nothing: no file is written and no playbook runs, and the
operator is told which site and which setting rather than reading it out of a
failed play.

Both controls stay. The role has to hold on its own against a hand-written
desired state; this one exists so an operator finds out at ``blitzecdn
validate``, before the fleet is touched.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from blitzecdn import __version__
from blitzecdn.core.plugins import (
    PluginMetadata,
    Severity,
    ValidationIssue,
    hookimpl,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane
    from blitzecdn.features.sites.domain import CdnSite


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="security",
        version=__version__,
        required=True,
        summary="Per-site request filtering and Under Attack Mode.",
    )


@hookimpl
def blitzecdn_deployment_checks(
    site: CdnSite, platform: ControlPlane
) -> Sequence[ValidationIssue]:
    """Refuse a deployment the challenge secret cannot possibly satisfy.

    Scoped to the two facts that make the failure certain: the site is enabled,
    and it asks for Under Attack Mode. A disabled site converges no server
    block, and a site that never asks is unaffected by the secret being absent
    — neither is blocked.
    """
    if not site.enabled or not site.under_attack_mode:
        return ()
    if platform.settings.under_attack_secret.get_secret_value():
        return ()
    return (
        ValidationIssue(
            plugin="security",
            site=site.name,
            message=(
                "under_attack_mode is on but BLITZE_UNDER_ATTACK_SECRET is not set "
                "on this controller, so the edge challenge capability cannot be "
                "enabled and the deployment would fail on every edge."
            ),
            severity=Severity.BLOCKING,
        ),
    )
