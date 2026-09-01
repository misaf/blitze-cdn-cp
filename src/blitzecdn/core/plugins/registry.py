"""The one place hook results are called for, checked, and made usable.

Pluggy hands back a list of whatever each implementation returned, in reverse
registration order, with ``None`` results dropped. That is the right primitive
and the wrong thing to spread through an application: every call site would
repeat the same flattening, the same "is this really an ``APIRouter``", and the
same reasoning about order.

So it is done once, here. Callers ask the registry for routers, jobs, checks or
state fragments and get a typed tuple in registration order. A plugin that
returns the wrong shape is rejected with a message naming the shape rather than
allowed to fail later inside FastAPI or APScheduler, where the traceback would
point at this repository and not at the plugin that caused it.

The registry is not a service locator. Nothing here resolves a *service*: it
resolves *contributions*, once, at composition time, and the things it hands
back are values.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pluggy

from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.plugins.discovery import PluginRejection
from blitzecdn.core.plugins.types import (
    CliCommandGroup,
    FleetStateContribution,
    HealthCheck,
    PluginMetadata,
    RuntimeContext,
    ScheduledJob,
    SiteStateContribution,
    StateValue,
    ValidationIssue,
    ValidationResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from fastapi import APIRouter

    from blitzecdn.bootstrap import ControlPlane
    from blitzecdn.features.sites.domain import CdnSite


def _flatten[T](results: Sequence[Any], hook: str, kind: type[T]) -> tuple[T, ...]:
    """One typed tuple, in registration order, from a hook's list of lists.

    Reversed because pluggy calls implementations last-registered-first, and
    the order features were registered in is the order an operator sees them:
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

    So ``BUILTIN_PLUGINS`` can be reordered freely and no edge converges
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
    """Everything the installed plugins contribute, resolved once."""

    def __init__(
        self,
        manager: pluggy.PluginManager,
        *,
        plugins: Sequence[PluginMetadata] = (),
        rejected: Sequence[PluginRejection] = (),
    ) -> None:
        self._manager = manager
        self.plugins = tuple(plugins)
        #: Optional plugins that were installed and did not load. Kept so an
        #: operator can be told why, rather than only a log line at startup.
        self.rejected = tuple(rejected)

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
        feature. Detaching a package that a configuration still asks for is a
        startup failure with the token in it, not a control plane that comes up
        and quietly serves the capability's absence as if it were its default.
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

    # --- registration-time contributions ---------------------------------

    def api_routers(self) -> tuple[APIRouter, ...]:
        from fastapi import APIRouter

        return _flatten(
            self._manager.hook.blitzecdn_api_routers(),
            "blitzecdn_api_routers",
            APIRouter,
        )

    def cli_commands(self) -> tuple[CliCommandGroup, ...]:
        return _flatten(
            self._manager.hook.blitzecdn_cli_commands(),
            "blitzecdn_cli_commands",
            CliCommandGroup,
        )

    def health_checks(self, platform: ControlPlane) -> tuple[HealthCheck, ...]:
        return _flatten(
            self._manager.hook.blitzecdn_health_checks(platform=platform),
            "blitzecdn_health_checks",
            HealthCheck,
        )

    def scheduled_jobs(self, platform: ControlPlane) -> dict[str, ScheduledJob]:
        """Every contributed job, by name.

        A mapping rather than a list because the worker resolves a job from a
        name that arrived in a queue message, and two plugins contributing one
        name would make that resolution silently pick one of them.
        """
        jobs: dict[str, ScheduledJob] = {}
        for job in _flatten(
            self._manager.hook.blitzecdn_scheduled_jobs(platform=platform),
            "blitzecdn_scheduled_jobs",
            ScheduledJob,
        ):
            if job.name in jobs:
                raise PluginError(f"two plugins contribute a job named {job.name!r}")
            jobs[job.name] = job
        return jobs

    # --- per-deployment contributions ------------------------------------

    def site_variables(
        self, site: CdnSite, platform: ControlPlane
    ) -> dict[str, StateValue]:
        contributions = _single(
            self._manager.hook.blitzecdn_site_desired_state(
                site=site, platform=platform
            ),
            "blitzecdn_site_desired_state",
            SiteStateContribution,
        )
        return merge_variables(
            contributions, subject=f"desired state for site {site.name!r}"
        )

    def fleet_variables(
        self, sites: tuple[CdnSite, ...], platform: ControlPlane
    ) -> dict[str, StateValue]:
        contributions = _single(
            self._manager.hook.blitzecdn_fleet_desired_state(
                sites=sites, platform=platform
            ),
            "blitzecdn_fleet_desired_state",
            FleetStateContribution,
        )
        return merge_variables(contributions, subject="fleet desired state")

    def validate_site(self, site: CdnSite, platform: ControlPlane) -> ValidationResult:
        missing = tuple(
            ValidationIssue(
                plugin="capabilities",
                site=site.name,
                message=(
                    f"capability {capability!r} is not installed; install a plugin "
                    "that provides it or disable the site setting that requests it"
                ),
            )
            for capability in self.missing(site.required_capabilities)
        )
        return ValidationResult(
            site=site.name,
            issues=missing
            + _flatten(
                self._manager.hook.blitzecdn_deployment_checks(
                    site=site, platform=platform
                ),
                "blitzecdn_deployment_checks",
                ValidationIssue,
            ),
        )

    def contributions_for(self, platform: ControlPlane) -> StateContributions:
        """The desired-state hooks with the platform they need already supplied.

        The renderer that calls these lives in `features/deployments` and has no
        business knowing what a plugin manager is, so it is handed this instead:
        two functions with the shape it actually needs. Binding here rather than
        passing the platform down through the deployment service also keeps the
        composition root the only thing holding both halves.
        """
        return StateContributions(self, platform)

    # --- lifecycle -------------------------------------------------------

    def startup(self, context: RuntimeContext, platform: ControlPlane) -> None:
        """Let every plugin do what it owes the starting process.

        Deliberately promises no order. A plugin whose startup depends on
        another plugin's startup has a dependency it has not declared and
        cannot declare here — that belongs in the composition root, where the
        two services are built in an order you can read.
        """
        self._manager.hook.blitzecdn_startup(context=context, platform=platform)

    def shutdown(self, context: RuntimeContext, platform: ControlPlane) -> None:
        self._manager.hook.blitzecdn_shutdown(context=context, platform=platform)


@dataclass(frozen=True, slots=True)
class StateContributions:
    """A registry and a platform, offering only what a renderer may ask for."""

    registry: PluginRegistry
    platform: ControlPlane

    def site_variables(self, site: CdnSite) -> dict[str, StateValue]:
        return self.registry.site_variables(site, self.platform)

    def fleet_variables(self, sites: tuple[CdnSite, ...]) -> dict[str, StateValue]:
        return self.registry.fleet_variables(sites, self.platform)


__all__ = ["PluginRegistry", "StateContributions", "merge_variables"]
