"""Register the security capability and its deployment guard."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from blitzecdn.core.plugins import (
    AnsibleContribution,
    PluginMetadata,
    Severity,
    ValidationIssue,
    hookimpl,
)
from blitzecdn_security import ansible
from blitzecdn_security.config import SECRET_VARIABLE, SecurityConfig

__version__ = "3.0.0"

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane
    from blitzecdn.features.sites.domain import CdnSite


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="security",
        version=__version__,
        summary="Per-site request filtering and Under Attack Mode.",
    )


@hookimpl
def blitzecdn_deployment_checks(
    site: CdnSite, platform: ControlPlane
) -> Sequence[ValidationIssue]:
    if not site.enabled or not site.under_attack_mode:
        return ()
    if SecurityConfig.from_settings(platform.settings).challenge_available:
        return ()
    return (
        ValidationIssue(
            plugin="security",
            site=site.name,
            message=(
                f"under_attack_mode is on but {SECRET_VARIABLE} is not set to at "
                "least 32 bytes on this controller, so the edge challenge "
                "capability cannot be enabled and the deployment would fail on "
                "every edge."
            ),
            severity=Severity.BLOCKING,
        ),
    )


@hookimpl
def blitzecdn_ansible_contributions() -> Sequence[AnsibleContribution]:
    return (
        AnsibleContribution(
            plugin="security",
            roles_path=ansible.ROLES_PATH,
            edge_roles=(ansible.EDGE_ROLE,),
        ),
    )
