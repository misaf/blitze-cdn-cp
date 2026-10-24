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

A built-in declares its :class:`Feature` in ``feature.py`` and builds the other
two in ``runtime.py`` from explicitly named collaborators; the composition root
calls those builders. An external plugin implements hooks instead, and those
hooks are handed an :class:`ExtensionContext` rather than the composition root.
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
    ProcessKind,
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
    from blitzecdn.core.config import Settings
    from blitzecdn.core.plugins.resolution.configuration import (
        ResolvedCapabilityEnvironment,
    )
    from blitzecdn.core.ports.operations import EventRecorder, PlaybookRunner

__all__ = [
    "ExtensionContext",
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

    ``startup`` and ``shutdown`` are told which process they run in, because
    the answer differs: only the API republishes interrupted deployments.
    """

    #: The contributing feature's `PluginMetadata.name`.
    plugin: str
    health_checks: tuple[HealthCheck, ...] = ()
    scheduled_jobs: tuple[ScheduledJob, ...] = ()
    startup: Callable[[ProcessKind], None] | None = None
    shutdown: Callable[[ProcessKind], None] | None = None


class SiteReader(Protocol):
    """Read-only access to derived virtual hosts."""

    def list_sites(self) -> list[CdnSite]: ...

    def get_site(self, name: str) -> CdnSite: ...


@dataclass(frozen=True, slots=True)
class ExtensionContext:
    """What an external plugin's hooks may use, and nothing else.

    Ports and configuration rather than services: an extension runs its own
    plays through ``fleet``, reads hosts through ``sites`` and records what it
    did through ``events``. A plugin reads its own configuration with
    ``capability_config.for_plugin(<its name>)``.
    """

    process: ProcessKind
    settings: Settings
    capability_config: ResolvedCapabilityEnvironment
    sites: SiteReader
    fleet: PlaybookRunner
    events: EventRecorder
    queue_ready: Callable[[], bool]
