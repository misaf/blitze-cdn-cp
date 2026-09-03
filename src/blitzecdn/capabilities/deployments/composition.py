"""How the deployments capability is built.

The same shape as :mod:`blitzecdn.capabilities.sites.composition` and the only
built-in with real assembly to do. ``DeploymentService`` is three dataclasses
and a renderer before it is a service, and that three-part shape is the
capability's own: what belongs in ``DeploymentPolicy`` rather than in
``DeploymentExecution`` is a question about deployments, answered by the people
changing deployments, and it was being answered in the composition root — the
one file that is supposed to know *which* concrete things are wired and not how
any capability is put together internally. Adding a collaborator here used to
mean editing ``bootstrap.py``.

The site validator lands here for the same reason. It is a two-line adapter
from the plugin registry to this capability's ``SiteValidator`` port, and the
port is the only thing that has ever wanted it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.capabilities.deployments.desired_state import DesiredStateRenderer
from blitzecdn.capabilities.deployments.ports import (
    DeploymentRequirements,
    DeploymentRunner,
    DeploymentStore,
    QueueBackgroundRunner,
    SiteRestore,
)
from blitzecdn.capabilities.deployments.service import (
    DeploymentExecution,
    DeploymentPersistence,
    DeploymentPolicy,
    DeploymentService,
)
from blitzecdn.capabilities.dns.ports import ZoneStore
from blitzecdn.capabilities.sites.domain import CdnSite
from blitzecdn.core.filesystem import atomic_write_yaml, read_log_tail
from blitzecdn.core.plugins import PluginRegistry, ValidationResult

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane

__all__ = ["build_deployment_service"]


class PluginSiteValidator:
    """The deployment service's view of "what do the plugins object to".

    A two-line class rather than a lambda so the service is handed something
    that reads like the port it declared, and so binding a registry to a
    platform stays in a composition module.
    """

    def __init__(self, plugins: PluginRegistry, platform: ControlPlane) -> None:
        self._plugins = plugins
        self._platform = platform

    def validate_site(self, site: CdnSite) -> ValidationResult:
        return self._plugins.validate_site(site, self._platform)


def build_deployment_service(
    platform: ControlPlane,
    *,
    deployments: DeploymentStore,
    zones: ZoneStore,
    sites: SiteRestore,
    requirements: DeploymentRequirements,
    runner: DeploymentRunner,
    background: QueueBackgroundRunner,
) -> DeploymentService:
    """Assemble the capability from its policy, its state, and its runners.

    ``runner`` and ``background`` arrive as arguments because choosing the
    Ansible adapter and the Dramatiq one is the composition root's decision and
    stays there. Everything else is put together here.

    The renderer is built here too. Every variable in a desired-state document
    comes from a plugin, and ``platform.plugins`` is what can answer for all of
    them — asking it here rather than in the service is what keeps the registry
    out of this capability's own modules.
    """
    return DeploymentService(
        policy=DeploymentPolicy(
            run_dir=platform.settings.run_dir,
            generated_vars_path=platform.settings.generated_vars_path,
            output_limit_bytes=platform.settings.output_limit_bytes,
            history_retention=platform.settings.history_retention,
            runtime_errors=platform.settings.validate_runtime,
        ),
        persistence=DeploymentPersistence(
            deployments=deployments,
            zones=zones,
            sites=sites,
            uow=platform.transactions,
            requirements=requirements,
        ),
        execution=DeploymentExecution(
            runner=runner,
            background=background,
            read_log=read_log_tail,
            renderer=DesiredStateRenderer(
                allow_empty_sites=platform.settings.allow_empty_sites,
                contributors=platform.plugins.contributions_for(platform),
                write_yaml=atomic_write_yaml,
            ),
            validator=PluginSiteValidator(platform.plugins, platform),
        ),
        events=platform.events,
        dns=platform.dns,
        workflows=platform.workflows,
    )
