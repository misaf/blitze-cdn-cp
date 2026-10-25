"""What one feature declares, renders and operates, as typed values.

A feature has three parts, bound at three different times, because their
dependencies exist at three different times:

* :class:`Feature` is a declaration. Routes, commands, Ansible roles,
  configuration and Nginx resources are known before any adapter exists, so
  the CLI tree, the API and the role search path can be assembled from them.
* :class:`FeatureRendering` feeds the release compiler. It may depend on
  configuration and adapters but never on a service, because what a site
  renders to must not change with what a service happens to hold.
* :class:`FeatureOperations` runs inside a process: health checks, scheduled
  jobs and lifecycle work, bound once every service has been built.

A feature declares its :class:`Feature` in ``feature.py`` and builds the other
two in ``runtime.py`` from explicitly named collaborators. The composition
root's roster names each feature module and the builders it binds.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from blitzecdn.core.plugins.types.ansible import AnsibleContribution
from blitzecdn.core.plugins.types.configuration import ConfigurationContribution
from blitzecdn.core.plugins.types.identity import PluginMetadata
from blitzecdn.core.plugins.types.nginx import NginxContribution
from blitzecdn.core.plugins.types.runtime import (
    CliCommandGroup,
    HealthCheck,
    ScheduledJob,
)
from blitzecdn.core.plugins.types.state import (
    FleetStateContribution,
    SiteStateContribution,
)
from blitzecdn.core.plugins.types.validation import ValidationIssue

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from fastapi import APIRouter

    from blitzecdn.capabilities.serving.domain import CdnSite

__all__ = [
    "Feature",
    "FeatureOperations",
    "FeatureRendering",
    "SiteReader",
]


@dataclass(frozen=True, slots=True)
class Feature:
    """Everything a feature contributes before any adapter or service exists."""

    metadata: PluginMetadata
    api_routers: tuple[APIRouter, ...] = ()
    cli_commands: tuple[CliCommandGroup, ...] = ()
    ansible: tuple[AnsibleContribution, ...] = ()
    configuration: tuple[ConfigurationContribution, ...] = ()
    nginx: tuple[NginxContribution, ...] = ()

    @property
    def name(self) -> str:
        return self.metadata.name


@dataclass(frozen=True, slots=True)
class FeatureRendering:
    """A feature's share of what the release compiler renders.

    ``site_state`` returns ``None`` when the feature has nothing to say about
    a site, so a feature costs nothing to merge for sites it does not touch.
    """

    #: The contributing feature's `PluginMetadata.name`.
    plugin: str
    site_state: Callable[[CdnSite], SiteStateContribution | None] | None = None
    fleet_state: (
        Callable[[tuple[CdnSite, ...]], FleetStateContribution | None] | None
    ) = None
    deployment_checks: Callable[[CdnSite], Sequence[ValidationIssue]] | None = None


@dataclass(frozen=True, slots=True)
class FeatureOperations:
    """What a feature does inside a running process.

    ``startup`` runs once per node, when the API process starts: it is the one
    process that lives as long as the node does, so recovery work such as
    republishing interrupted deployments belongs there. ``shutdown`` runs when
    the API or the worker stops.
    """

    #: The contributing feature's `PluginMetadata.name`.
    plugin: str
    health_checks: tuple[HealthCheck, ...] = ()
    scheduled_jobs: tuple[ScheduledJob, ...] = ()
    startup: Callable[[], None] | None = None
    shutdown: Callable[[], None] | None = None


class SiteReader(Protocol):
    """Read-only access to derived virtual hosts."""

    def list_sites(self) -> list[CdnSite]: ...

    def get_site(self, name: str) -> CdnSite: ...
