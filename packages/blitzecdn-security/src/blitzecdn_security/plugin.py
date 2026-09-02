"""Register the security capability and what it refuses to deploy.

The deployment check is this capability's half of a control its own edge role
also holds. ``blitzecdn_security`` — the role beside this module, which core's
edge play runs because the contribution below says so — asserts that no edge
enables the challenge without a secret of its own, and that assertion is the
last line: it fires after the desired-state document is written and a play has
started. The same question
answered here costs nothing: no file is written and no playbook runs, and the
operator is told which site and which setting rather than reading it out of a
failed play.

Both controls stay. The role has to hold on its own against a hand-written
desired state; this one exists so an operator finds out at ``blitzecdn
validate``, before the fleet is touched.

The njs implementation, fleet secret, global import, request filters and
challenge locations all ship in this wheel. Core provides only typed, stable
Nginx insertion contexts and renders the resources discovered from this plugin.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from blitzecdn.core.plugins import (
    AnsibleContribution,
    NginxContribution,
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
        required=False,
        provides=frozenset({"security"}),
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
    # The role, and the fact that the edge play should run it. Core adds the
    # directory to Ansible's role search path, adds the name to the play's
    # capability slot, and never learns what either contains.
    return (
        AnsibleContribution(
            plugin="security",
            roles_path=ansible.ROLES_PATH,
            edge_roles=(ansible.EDGE_ROLE,),
            environment_keys=(SECRET_VARIABLE,),
        ),
    )


@hookimpl
def blitzecdn_nginx_contributions() -> Sequence[NginxContribution]:
    return (
        NginxContribution(
            plugin="security",
            templates_path=Path(__file__).with_name("nginx"),
            http_fragments=("security-http.conf.j2",),
            server_fragments=("security-server.conf.j2",),
            access_fragments=("security-access.conf.j2",),
            upstream_fragments=("security-upstream.conf.j2",),
        ),
    )
