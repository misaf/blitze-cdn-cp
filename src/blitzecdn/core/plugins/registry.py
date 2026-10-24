"""The one place features and external hooks become typed contributions.

Built-in features declare their contributions as values. External plugins
answer pluggy hooks, which hand back a list of whatever each implementation
returned, in reverse registration order, with ``None`` results dropped. The
registry merges both into one typed answer per question, built-ins first and
then external plugins, each in registration order. A plugin that returns the
wrong shape is rejected with a message naming the shape rather than allowed to
fail later inside FastAPI or the scheduler.

Rendering and operations are bound in two stages, because their collaborators
exist at two different times:
:meth:`~blitzecdn.core.plugins.PluginRegistry.bind_rendering` before the
release compiler is built, and
:meth:`~blitzecdn.core.plugins.PluginRegistry.bind_operations` after every
service is. Neither stage hands anything the composition root.

The registry is not a service locator. Nothing here resolves a *service*: it
resolves *contributions*, and the things it hands back are values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pluggy

import blitzecdn.core.plugins.hooks as hookspecs
from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.plugins.discovery import PluginRejection
from blitzecdn.core.plugins.types import (
    PROJECT_NAME,
    AnsibleContribution,
    CliCommandGroup,
    ConfigurationContribution,
    ExtensionContext,
    Feature,
    FeatureOperations,
    FeatureRendering,
    FleetStateContribution,
    HealthCheck,
    NginxContribution,
    PluginMetadata,
    ScheduledJob,
    SiteStateContribution,
    StateValue,
    ValidationIssue,
    ValidationResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from fastapi import APIRouter

    from blitzecdn.capabilities.serving.domain import CdnSite


def _flatten[T](results: Sequence[Any], hook: str, kind: type[T]) -> tuple[T, ...]:
    """One typed tuple, in registration order, from a hook's list of lists.

    Reversed because pluggy calls implementations last-registered-first, and
    the order capabilities were registered in is the order an operator sees them:
    the command tree and the published route list should not depend on an
    implementation detail of the hook caller.
    """
    flat: list[T] = []
    for result in reversed(results):
        if isinstance(result, kind):
            flat.append(result)
            continue
        if isinstance(result, str) or not isinstance(result, Iterable):
            raise PluginError(
                f"{hook} must return a sequence of {kind.__name__}, "
                f"got {type(result).__name__}"
            )
        for item in result:
            if not isinstance(item, kind):
                raise PluginError(
                    f"{hook} returned a {type(item).__name__} "
                    f"where a {kind.__name__} was expected"
                )
            flat.append(item)
    return tuple(flat)


def _single[T](results: Sequence[Any], hook: str, kind: type[T]) -> tuple[T, ...]:
    """A hook whose implementations each return one value, or ``None``."""
    for result in results:
        if not isinstance(result, kind):
            raise PluginError(
                f"{hook} must return a {kind.__name__} or None, "
                f"got {type(result).__name__}"
            )
    return tuple(reversed(results))


def merge_variables(
    contributions: Sequence[SiteStateContribution] | Sequence[FleetStateContribution],
    *,
    subject: str,
) -> dict[str, StateValue]:
    """Merge every plugin's variables into one document, order-independently.

    Two plugins writing one variable is a conflict, not a race won by whichever
    registered last. The single exception is a declared override: a plugin may
    say it is deliberately replacing a value another produced — ``certificates``
    replaces the certificate paths projected from the site model, because the
    installed certificate lives on this controller and the model only knows
    where it will land on the edge. Exactly one plugin may claim that per
    variable, and claiming a variable nobody else writes is fine: whether the
    site model happens to carry a path is not something the certificate plugin
    should have to know.

    So ``BUILTIN_FEATURES`` can be reordered freely and no edge converges
    differently. Registration order decides presentation and nothing else. Key
    *order* still follows first appearance, because the rendered YAML is read by
    people and a stable layout is worth having.
    """
    plain: dict[str, list[tuple[str, StateValue]]] = {}
    claimed: dict[str, list[tuple[str, StateValue]]] = {}
    order: list[str] = []
    for contribution in contributions:
        for key, value in contribution.variables.items():
            bucket = claimed if key in contribution.overrides else plain
            bucket.setdefault(key, []).append((contribution.plugin, value))
            if key not in order:
                order.append(key)
    merged: dict[str, StateValue] = {}
    for key in order:
        overriding = claimed.get(key, [])
        writers = plain.get(key, [])
        if len(overriding) > 1:
            raise PluginError(
                f"{subject}: {', '.join(sorted(name for name, _ in overriding))} "
                f"each claim to override {key!r}; exactly one plugin may"
            )
        if not overriding and len(writers) > 1:
            raise PluginError(
                f"{subject}: {', '.join(sorted(name for name, _ in writers))} "
                f"both set {key!r}; one of them must declare it in `overrides` "
                "to say which wins"
            )
        merged[key] = (overriding or writers)[0][1]
    return merged


class PluginRegistry:
    """Every installed feature and external plugin, and what each declares."""

    def __init__(
        self,
        features: Sequence[Feature] = (),
        *,
        manager: pluggy.PluginManager | None = None,
        external: Sequence[PluginMetadata] = (),
        rejected: Sequence[PluginRejection] = (),
    ) -> None:
        self.features = tuple(features)
        self._manager = manager if manager is not None else _empty_manager()
        self.plugins = (*(feature.metadata for feature in self.features), *external)
        #: External plugins that were installed and did not load. Kept so an
        #: operator can be told why, rather than only a log line at startup.
        self.rejected = tuple(rejected)
        seen: set[str] = set()
        for plugin in self.plugins:
            if plugin.name in seen:
                raise PluginError(f"two plugins claim the name {plugin.name!r}")
            seen.add(plugin.name)

    def __contains__(self, name: str) -> bool:
        return any(plugin.name == name for plugin in self.plugins)

    @property
    def capabilities(self) -> frozenset[str]:
        """Every capability token the installed plugins answer for.

        The union of what each plugin declared, and the only thing anything in
        core asks about "is that capability here?". Core never learns which
        distribution supplied a token, which is what keeps
        `if compression_installed:` from being writable in the first place.
        """
        return frozenset().union(*(plugin.capabilities for plugin in self.plugins))

    def missing(self, required: Iterable[str]) -> tuple[str, ...]:
        """Which of ``required`` no installed plugin provides, sorted."""
        return tuple(sorted(set(required) - self.capabilities))

    def require(self, required: Iterable[str], *, subject: str) -> None:
        """Refuse to continue while a named capability is not installed.

        Deterministic and generic: the tokens come from configuration, the
        answer comes from plugin metadata, and nothing between them names a
        capability. A configuration that asks for a capability no installed
        plugin provides is a startup failure with the token in it, not a
        control plane that comes up and quietly serves its absence.
        """
        absent = self.missing(required)
        if absent:
            installed = ", ".join(sorted(self.capabilities)) or "none"
            raise PluginError(
                f"{subject} requires the "
                f"{'capabilities' if len(absent) > 1 else 'capability'} "
                f"{', '.join(absent)}, which no installed plugin provides. "
                f"Installed capabilities: {installed}. Install the distribution "
                "that supplies it, or remove it from `required_capabilities`."
            )

    # --- declarations ------------------------------------------------------

    def api_routers(self) -> tuple[APIRouter, ...]:
        from fastapi import APIRouter

        return (
            *(router for feature in self.features for router in feature.api_routers),
            *_flatten(
                self._manager.hook.blitzecdn_api_routers(),
                "blitzecdn_api_routers",
                APIRouter,
            ),
        )

    def cli_commands(self) -> tuple[CliCommandGroup, ...]:
        return (
            *(group for feature in self.features for group in feature.cli_commands),
            *_flatten(
                self._manager.hook.blitzecdn_cli_commands(),
                "blitzecdn_cli_commands",
                CliCommandGroup,
            ),
        )

    def ansible_contributions(self) -> tuple[AnsibleContribution, ...]:
        """Every installed plugin's Ansible roles directory, in a fixed order.

        Registration order, like the routers and the commands. The search path
        this feeds is composed in :mod:`blitzecdn.core.plugins.resolution`,
        which is where determinism and shadowing are actually decided.
        """
        return (
            *(item for feature in self.features for item in feature.ansible),
            *_flatten(
                self._manager.hook.blitzecdn_ansible_contributions(),
                "blitzecdn_ansible_contributions",
                AnsibleContribution,
            ),
        )

    def configuration_contributions(self) -> tuple[ConfigurationContribution, ...]:
        """Every installed plugin's claim on `BLITZE_*` names it configures.

        Read once, before any adapter exists, because several adapters are
        built from what it resolves to. Ownership, collisions and unclaimed
        names are decided in :mod:`blitzecdn.core.plugins.resolution`.
        """
        return (
            *(item for feature in self.features for item in feature.configuration),
            *_flatten(
                self._manager.hook.blitzecdn_capability_configuration(),
                "blitzecdn_capability_configuration",
                ConfigurationContribution,
            ),
        )

    def nginx_contributions(self) -> tuple[NginxContribution, ...]:
        """Every installed package's static Nginx resources."""
        return (
            *(item for feature in self.features for item in feature.nginx),
            *_flatten(
                self._manager.hook.blitzecdn_nginx_contributions(),
                "blitzecdn_nginx_contributions",
                NginxContribution,
            ),
        )

    # --- binding -----------------------------------------------------------

    def bind_rendering(
        self, renderings: Sequence[FeatureRendering], context: ExtensionContext
    ) -> Rendering:
        """What the release compiler asks every installed plugin, bound once."""
        self._check_bound(renderings, "rendering")
        return Rendering(self, tuple(renderings), context)

    def bind_operations(
        self, operations: Sequence[FeatureOperations], context: ExtensionContext
    ) -> Operations:
        """Every health check, job and lifecycle step, resolved once.

        External hooks are asked here, once, so a job's callable and a check's
        probe are the same objects for the lifetime of the process.
        """
        self._check_bound(operations, "operations")
        checks = (
            *(check for item in operations for check in item.health_checks),
            *_flatten(
                self._manager.hook.blitzecdn_health_checks(context=context),
                "blitzecdn_health_checks",
                HealthCheck,
            ),
        )
        jobs = (
            *(job for item in operations for job in item.scheduled_jobs),
            *_flatten(
                self._manager.hook.blitzecdn_scheduled_jobs(context=context),
                "blitzecdn_scheduled_jobs",
                ScheduledJob,
            ),
        )
        return Operations(
            health_checks=_unique_checks(checks),
            scheduled_jobs=_unique_jobs(jobs),
            operations=tuple(operations),
            manager=self._manager,
            context=context,
        )

    def _check_bound(
        self, bound: Sequence[FeatureRendering] | Sequence[FeatureOperations], kind: str
    ) -> None:
        """Bound behaviour belongs to exactly one declared built-in feature."""
        features = {feature.name for feature in self.features}
        seen: set[str] = set()
        for item in bound:
            if item.plugin not in features:
                raise PluginError(
                    f"{kind} for {item.plugin!r} names no built-in feature"
                )
            if item.plugin in seen:
                raise PluginError(f"{item.plugin!r} binds its {kind} twice")
            seen.add(item.plugin)

    def _missing_capability_issues(self, site: CdnSite) -> tuple[ValidationIssue, ...]:
        requested = site.policy.capability_requirements
        return tuple(
            ValidationIssue(
                plugin="capabilities",
                site=site.name,
                message=(
                    f"capability {capability!r} is not installed, and this "
                    f"site's {', '.join(requested[capability])} "
                    f"{'request' if len(requested[capability]) > 1 else 'requests'}"
                    " it; install a plugin that provides it or disable the "
                    "site setting that requests it"
                ),
            )
            for capability in self.missing(requested)
        )


class Rendering:
    """Every plugin's share of a release, asked per site or per fleet."""

    def __init__(
        self,
        registry: PluginRegistry,
        renderings: tuple[FeatureRendering, ...],
        context: ExtensionContext,
    ) -> None:
        self._registry = registry
        self._renderings = renderings
        self._hook = registry._manager.hook
        self._context = context

    def site_variables(self, site: CdnSite) -> dict[str, StateValue]:
        contributions = [
            contribution
            for rendering in self._renderings
            if rendering.site_state is not None
            for contribution in [rendering.site_state(site)]
            if contribution is not None
        ]
        contributions.extend(
            _single(
                self._hook.blitzecdn_site_desired_state(
                    site=site, context=self._context
                ),
                "blitzecdn_site_desired_state",
                SiteStateContribution,
            )
        )
        return merge_variables(
            contributions, subject=f"desired state for site {site.name!r}"
        )

    def fleet_variables(self, sites: tuple[CdnSite, ...]) -> dict[str, StateValue]:
        contributions = [
            contribution
            for rendering in self._renderings
            if rendering.fleet_state is not None
            for contribution in [rendering.fleet_state(sites)]
            if contribution is not None
        ]
        contributions.extend(
            _single(
                self._hook.blitzecdn_fleet_desired_state(
                    sites=sites, context=self._context
                ),
                "blitzecdn_fleet_desired_state",
                FleetStateContribution,
            )
        )
        return merge_variables(contributions, subject="fleet desired state")

    def validate_site(self, site: CdnSite) -> ValidationResult:
        """Every reason this site cannot be deployed by this installation.

        The capability issues come first and are generic: the tokens are the
        site's own `capability_requirements`, the answer is plugin metadata,
        and the settings that asked are quoted straight out of the mapping.

        Both halves, which is what a caller asking "can this installation serve
        this site" wants. The release compiler asks a narrower question and
        uses :meth:`site_objections` instead — see there for why.
        """
        return ValidationResult(
            site=site.name,
            issues=self._registry._missing_capability_issues(site)
            + self.site_objections(site),
        )

    def site_objections(self, site: CdnSite) -> tuple[ValidationIssue, ...]:
        """What the *installed* plugins object to about this site.

        Separate from :meth:`validate_site` because the two answers have
        different consequences. A capability that is missing cannot be asked
        for anything and simply contributes nothing; a capability that is
        installed and objects must not then be asked to render this site,
        because the objection is usually the reason asking would fail.

        The release compiler makes the missing-capability check itself, against
        the edges as well as against this controller, so asking for both here
        would report each of them twice.
        """
        return (
            *(
                issue
                for rendering in self._renderings
                if rendering.deployment_checks is not None
                for issue in rendering.deployment_checks(site)
            ),
            *_flatten(
                self._hook.blitzecdn_deployment_checks(
                    site=site, context=self._context
                ),
                "blitzecdn_deployment_checks",
                ValidationIssue,
            ),
        )


@dataclass(frozen=True, slots=True)
class Operations:
    """Every health check, scheduled job and lifecycle step in this process."""

    health_checks: tuple[HealthCheck, ...]
    #: By name, because the worker resolves a job from a name that arrived in a
    #: queue message.
    scheduled_jobs: Mapping[str, ScheduledJob]
    operations: tuple[FeatureOperations, ...]
    manager: pluggy.PluginManager
    context: ExtensionContext

    def startup(self) -> None:
        """Let every plugin do what it owes the starting process.

        Deliberately promises no order. A plugin whose startup depends on
        another plugin's startup has a dependency it has not declared and
        cannot declare here — that belongs in the composition root, where the
        two services are built in an order you can read.
        """
        for item in self.operations:
            if item.startup is not None:
                item.startup(self.context.process)
        self.manager.hook.blitzecdn_startup(context=self.context)

    def shutdown(self) -> None:
        """The mirror of :meth:`startup`, in reverse order."""
        self.manager.hook.blitzecdn_shutdown(context=self.context)
        for item in reversed(self.operations):
            if item.shutdown is not None:
                item.shutdown(self.context.process)


def _unique_checks(checks: Sequence[HealthCheck]) -> tuple[HealthCheck, ...]:
    """Refuse two checks that answer to one name.

    `/health` reports a check by `name`, so two plugins contributing
    `"database"` would put two entries under one label and an operator reading
    a failure would have no way to tell which wheel's check is red.
    """
    seen: dict[str, str] = {}
    for check in checks:
        if check.name in seen:
            raise PluginError(
                f"plugins {seen[check.name]!r} and {check.plugin!r} both "
                f"contribute a health check named {check.name!r}"
            )
        seen[check.name] = check.plugin
    return tuple(checks)


def _unique_jobs(jobs: Sequence[ScheduledJob]) -> dict[str, ScheduledJob]:
    """Every job by name, refusing two that would resolve to one."""
    table: dict[str, ScheduledJob] = {}
    for job in jobs:
        if job.name in table:
            raise PluginError(
                f"plugins {table[job.name].plugin!r} and {job.plugin!r} both "
                f"contribute a job named {job.name!r}"
            )
        table[job.name] = job
    return table


def _empty_manager() -> pluggy.PluginManager:
    manager = pluggy.PluginManager(PROJECT_NAME)
    manager.add_hookspecs(hookspecs)
    return manager


__all__ = ["Operations", "PluginRegistry", "Rendering", "merge_variables"]
