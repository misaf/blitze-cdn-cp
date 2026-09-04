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
from typing import TYPE_CHECKING

from blitzecdn.core.plugins import (
    AnsibleContribution,
    ConfigurationContribution,
    EdgeModule,
    EnvironmentKey,
    NginxContribution,
    PluginMetadata,
    Severity,
    ValidationIssue,
    hookimpl,
)
from blitzecdn.core.runtime.resources import distribution_version, package_directory
from blitzecdn_security import ansible
from blitzecdn_security.config import (
    MINIMUM_SECRET_BYTES,
    SECRET_VARIABLE,
    SecurityConfig,
)

#: This distribution's version, asked of the environment rather than
#: written down here: it is what ``PluginMetadata.version`` reports and
#: what ``blitzecdn plugins`` shows an operator, so the one number that
#: must not drift from ``pyproject.toml`` is not copied out of it.
__version__ = distribution_version(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.capabilities.sites.domain import CdnSite
    from blitzecdn.composition import ControlPlane


#: The Jinja fragments this capability contributes to the edge's Nginx
#: configuration, resolved under the same guard its roles are. A sibling of
#: ``ansible/`` rather than a child: core's ``blitzecdn_nginx`` renders these
#: from the resolved contribution, so they are not part of any role this
#: package ships.
NGINX_TEMPLATES = (
    package_directory(
        __name__,
        resolves="Nginx templates are rendered from a filesystem path",
    )
    / "nginx"
)


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
    config = SecurityConfig.from_capability_config(
        platform.capability_config.for_plugin("security")
    )
    if config.challenge_available:
        return ()
    return (
        ValidationIssue(
            plugin="security",
            site=site.name,
            message=(
                f"under_attack_mode is on but {SECRET_VARIABLE} is not set on "
                "this controller, so the edge challenge capability cannot be "
                "enabled and the deployment would fail on every edge."
            ),
            severity=Severity.BLOCKING,
        ),
    )


@hookimpl
def blitzecdn_capability_configuration() -> Sequence[ConfigurationContribution]:
    """Claim the fleet-wide key the Under Attack Mode challenge is signed with.

    Not `required`: a controller with no signing secret is a working control
    plane with one site setting it will refuse, which is a deployment check
    and not a startup failure. The length is core's to enforce, and it is
    declared rather than re-checked in this package, so a placeholder is
    refused at composition instead of at the first site that asks for the
    challenge.
    """
    return (
        ConfigurationContribution(
            plugin="security",
            environment_keys=(
                EnvironmentKey(
                    name=SECRET_VARIABLE,
                    minimum_bytes=MINIMUM_SECRET_BYTES,
                    summary=(
                        "The fleet-wide key the Under Attack Mode challenge is "
                        "signed with."
                    ),
                ),
            ),
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
            # Under Attack Mode is an njs script, so this capability is the
            # one that needs the JavaScript engine. `build=False`: njs ships
            # in the official Alpine image already, and asking pkg-oss to
            # build a module the base has installed fails the image build.
            edge_modules=(
                EdgeModule(
                    name="njs",
                    objects=("ngx_http_js_module.so",),
                    build=False,
                    probe="js_path /etc/nginx;",
                ),
            ),
        ),
    )


@hookimpl
def blitzecdn_nginx_contributions() -> Sequence[NginxContribution]:
    return (
        NginxContribution(
            plugin="security",
            templates_path=NGINX_TEMPLATES,
            http_fragments=("security-http.conf.j2",),
            server_fragments=("security-server.conf.j2",),
            access_fragments=("security-access.conf.j2",),
            upstream_fragments=("security-upstream.conf.j2",),
        ),
    )
