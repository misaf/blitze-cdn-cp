"""What one feature declares, renders and operates, as typed values.

A feature has three parts, bound at three different times, because their
dependencies exist at three different times:

* :class:`Feature` is a declaration. Routes, commands, Ansible roles,
  configuration, Nginx resources and site-policy contracts are known before
  any adapter exists, so the CLI tree, the API, the role search path and the
  site policy can be assembled from them.
* :class:`FeatureRendering` feeds the release compiler. It may depend on
  configuration and adapters but never on a service, because what a site
  renders to must not change with what a service happens to hold.
* :class:`FeatureOperations` runs inside a process: health checks, scheduled
  jobs and lifecycle work, bound once every service has been built.

A feature declares its :class:`Feature` in ``feature.py`` and builds the other
two in ``wiring.py`` from explicitly named collaborators. The composition
root's roster names each feature module and the builders it binds.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from blitzecdn.core.features.types.ansible import AnsibleContribution
from blitzecdn.core.features.types.configuration import ConfigurationContribution
from blitzecdn.core.features.types.identity import FeatureMetadata
from blitzecdn.core.features.types.nginx import NginxContribution
from blitzecdn.core.features.types.policy import PolicyContribution
from blitzecdn.core.features.types.runtime import (
    CliCommandGroup,
    HealthCheck,
    ScheduledJob,
)
from blitzecdn.core.features.types.state import (
    FleetStateContribution,
    SiteStateContribution,
)
from blitzecdn.core.features.types.validation import ValidationIssue

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from fastapi import APIRouter

__all__ = [
    "Feature",
    "FeatureOperations",
    "FeatureRendering",
    "RequestsCapabilities",
    "Site",
]


class RequestsCapabilities(Protocol):
    """The one question core asks of a site's policy."""

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """Capability tokens the policy requests, and the settings that asked."""
        ...


class Site(Protocol):
    """What core asks of a site: its name, and what its policy requires.

    A site's concrete model is composed from every capability's contract, so
    core cannot name it without naming them. It names this instead, and the
    features that render a site receive the concrete model the composition
    root binds them to.
    """

    @property
    def name(self) -> str: ...

    @property
    def policy(self) -> RequestsCapabilities: ...


@dataclass(frozen=True, slots=True)
class Feature:
    """Everything a feature contributes before any adapter or service exists."""

    metadata: FeatureMetadata
    api_routers: tuple[APIRouter, ...] = ()
    cli_commands: tuple[CliCommandGroup, ...] = ()
    ansible: tuple[AnsibleContribution, ...] = ()
    configuration: tuple[ConfigurationContribution, ...] = ()
    nginx: tuple[NginxContribution, ...] = ()
    site_policy: tuple[PolicyContribution, ...] = ()

    @property
    def name(self) -> str:
        return self.metadata.name


@dataclass(frozen=True, slots=True)
class FeatureRendering[S: Site]:
    """A feature's share of what the release compiler renders, per site type.

    ``site_state`` returns ``None`` when the feature has nothing to say about
    a site, so a feature costs nothing to merge for sites it does not touch.
    """

    #: The contributing feature's `FeatureMetadata.name`.
    feature: str
    site_state: Callable[[S], SiteStateContribution | None] | None = None
    fleet_state: Callable[[tuple[S, ...]], FleetStateContribution | None] | None = None
    deployment_checks: Callable[[S], Sequence[ValidationIssue]] | None = None


@dataclass(frozen=True, slots=True)
class FeatureOperations:
    """What a feature does inside a running process.

    ``startup`` runs once per node, when the API process starts: it is the one
    process that lives as long as the node does, so recovery work such as
    republishing interrupted deployments belongs there. ``shutdown`` runs when
    the API or the worker stops.
    """

    #: The contributing feature's `FeatureMetadata.name`.
    feature: str
    health_checks: tuple[HealthCheck, ...] = ()
    scheduled_jobs: tuple[ScheduledJob, ...] = ()
    startup: Callable[[], None] | None = None
    shutdown: Callable[[], None] | None = None
