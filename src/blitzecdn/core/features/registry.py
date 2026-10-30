"""The one place features become typed contributions.

Features declare their contributions as values; the registry answers each
question across all of them, in registration order.

Rendering and operations are bound in two stages, because their collaborators
exist at two different times:
:meth:`~blitzecdn.core.features.FeatureRegistry.bind_rendering` before the
release compiler is built, and
:meth:`~blitzecdn.core.features.FeatureRegistry.bind_operations` after every
service is. Neither stage hands anything the composition root.

The registry is not a service locator. Nothing here resolves a *service*: it
resolves *contributions*, and the things it hands back are values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from blitzecdn.core.exceptions import FeatureError
from blitzecdn.core.features.types import (
    AnsibleContribution,
    CliCommandGroup,
    ConfigurationContribution,
    Feature,
    FeatureOperations,
    FeatureRendering,
    FleetStateContribution,
    HealthCheck,
    NginxContribution,
    PolicyContribution,
    ScheduledJob,
    Site,
    SiteStateContribution,
    StateValue,
    ValidationIssue,
    ValidationResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from fastapi import APIRouter


def merge_variables(
    contributions: Sequence[SiteStateContribution] | Sequence[FleetStateContribution],
    *,
    subject: str,
) -> dict[str, StateValue]:
    """Merge every feature's variables into one document, order-independently.

    Two features writing one variable is a conflict, not a race won by whichever
    registered last, unless exactly one of them declares it in ``overrides`` —
    as ``certificates`` does for the certificate paths the site model projects.
    So reordering ``BUILTIN_FEATURES`` never changes what an edge converges to;
    it changes only key order, which follows first appearance for readable YAML.
    """
    plain: dict[str, list[tuple[str, StateValue]]] = {}
    claimed: dict[str, list[tuple[str, StateValue]]] = {}
    order: list[str] = []
    for contribution in contributions:
        for key, value in contribution.variables.items():
            bucket = claimed if key in contribution.overrides else plain
            bucket.setdefault(key, []).append((contribution.feature, value))
            if key not in order:
                order.append(key)
    merged: dict[str, StateValue] = {}
    for key in order:
        overriding = claimed.get(key, [])
        writers = plain.get(key, [])
        if len(overriding) > 1:
            raise FeatureError(
                f"{subject}: {', '.join(sorted(name for name, _ in overriding))} "
                f"each claim to override {key!r}; exactly one feature may"
            )
        if not overriding and len(writers) > 1:
            raise FeatureError(
                f"{subject}: {', '.join(sorted(name for name, _ in writers))} "
                f"both set {key!r}; one of them must declare it in `overrides` "
                "to say which wins"
            )
        merged[key] = (overriding or writers)[0][1]
    return merged


class FeatureRegistry:
    """Every feature, and what each declares."""

    def __init__(self, features: Sequence[Feature] = ()) -> None:
        self.features = tuple(features)
        self.metadata = tuple(feature.metadata for feature in self.features)
        seen: set[str] = set()
        for feature in self.metadata:
            if feature.name in seen:
                raise FeatureError(f"two features claim the name {feature.name!r}")
            seen.add(feature.name)

    def __contains__(self, name: str) -> bool:
        return any(feature.name == name for feature in self.metadata)

    @property
    def capabilities(self) -> frozenset[str]:
        """Every capability token the registered features provide.

        The only answer core has to "is that capability here?": it never learns
        which feature supplied a token, so `if compression_installed:` cannot be
        written.
        """
        return frozenset().union(*(feature.capabilities for feature in self.metadata))

    def missing(self, required: Iterable[str]) -> tuple[str, ...]:
        """Which of ``required`` no installed feature provides, sorted."""
        return tuple(sorted(set(required) - self.capabilities))

    # --- declarations ------------------------------------------------------

    def api_routers(self) -> tuple[APIRouter, ...]:
        return tuple(
            router for feature in self.features for router in feature.api_routers
        )

    def cli_commands(self) -> tuple[CliCommandGroup, ...]:
        return tuple(
            group for feature in self.features for group in feature.cli_commands
        )

    def ansible_contributions(self) -> tuple[AnsibleContribution, ...]:
        """Every feature's Ansible roles directory, in registration order.

        The search path is composed, and shadowing decided, in
        :mod:`blitzecdn.core.features.resolution`.
        """
        return tuple(item for feature in self.features for item in feature.ansible)

    def configuration_contributions(self) -> tuple[ConfigurationContribution, ...]:
        """Every feature's claim on the `BLITZE_*` names it configures.

        Ownership, collisions and unclaimed names are decided in
        :mod:`blitzecdn.core.features.resolution`.
        """
        return tuple(
            item for feature in self.features for item in feature.configuration
        )

    def nginx_contributions(self) -> tuple[NginxContribution, ...]:
        """Every feature's static Nginx resources."""
        return tuple(item for feature in self.features for item in feature.nginx)

    def policy_contributions(self) -> tuple[PolicyContribution, ...]:
        """Every feature's site-policy contracts, in registration order.

        Ownership and dependencies are decided in
        :mod:`blitzecdn.core.features.resolution`.
        """
        return tuple(item for feature in self.features for item in feature.site_policy)

    # --- binding -----------------------------------------------------------

    def bind_rendering[S: Site](
        self, renderings: Sequence[FeatureRendering[S]]
    ) -> Rendering[S]:
        """What the release compiler asks every feature, bound once."""
        self._check_bound(renderings, "rendering")
        return Rendering(self, tuple(renderings))

    def bind_operations(self, operations: Sequence[FeatureOperations]) -> Operations:
        """Every health check, job and lifecycle step, resolved once.

        Resolved here, once, so a job's callable and a check's probe are the
        same objects for the lifetime of the process.
        """
        self._check_bound(operations, "operations")
        return Operations(
            health_checks=_unique_checks(
                [check for item in operations for check in item.health_checks]
            ),
            scheduled_jobs=_unique_jobs(
                [job for item in operations for job in item.scheduled_jobs]
            ),
            operations=tuple(operations),
        )

    def _check_bound(
        self,
        bound: Sequence[FeatureRendering[Any]] | Sequence[FeatureOperations],
        kind: str,
    ) -> None:
        """Bound behaviour belongs to exactly one declared built-in feature."""
        features = {feature.name for feature in self.features}
        seen: set[str] = set()
        for item in bound:
            if item.feature not in features:
                raise FeatureError(
                    f"{kind} for {item.feature!r} names no built-in feature"
                )
            if item.feature in seen:
                raise FeatureError(f"{item.feature!r} binds its {kind} twice")
            seen.add(item.feature)

    def _missing_capability_issues(self, site: Site) -> tuple[ValidationIssue, ...]:
        requested = site.policy.capability_requirements
        return tuple(
            ValidationIssue(
                feature="capabilities",
                site=site.name,
                message=(
                    f"capability {capability!r} is not installed, and this "
                    f"site's {', '.join(requested[capability])} "
                    f"{'request' if len(requested[capability]) > 1 else 'requests'}"
                    " it; upgrade to a controller that provides it or disable the "
                    "site setting that requests it"
                ),
            )
            for capability in self.missing(requested)
        )


class Rendering[S: Site]:
    """Every feature's share of a release, asked per site or per fleet."""

    def __init__(
        self, registry: FeatureRegistry, renderings: tuple[FeatureRendering[S], ...]
    ) -> None:
        self._registry = registry
        self._renderings = renderings

    def site_variables(self, site: S) -> dict[str, StateValue]:
        contributions = [
            contribution
            for rendering in self._renderings
            if rendering.site_state is not None
            for contribution in [rendering.site_state(site)]
            if contribution is not None
        ]
        return merge_variables(
            contributions, subject=f"desired state for site {site.name!r}"
        )

    def fleet_variables(self, sites: tuple[S, ...]) -> dict[str, StateValue]:
        contributions = [
            contribution
            for rendering in self._renderings
            if rendering.fleet_state is not None
            for contribution in [rendering.fleet_state(sites)]
            if contribution is not None
        ]
        return merge_variables(contributions, subject="fleet desired state")

    def validate_site(self, site: S) -> ValidationResult:
        """Every reason this installation cannot deploy this site.

        Missing capabilities first, then what the features object to. The
        release compiler uses :meth:`site_objections` alone; see there for why.
        """
        return ValidationResult(
            site=site.name,
            issues=self._registry._missing_capability_issues(site)
            + self.site_objections(site),
        )

    def site_objections(self, site: S) -> tuple[ValidationIssue, ...]:
        """What the registered features object to about this site.

        Separate because an objecting feature must not then be asked to render
        the site, while a missing one simply contributes nothing. The release
        compiler checks missing capabilities itself, against the edges too, so
        asking for both here would report each twice.
        """
        return tuple(
            issue
            for rendering in self._renderings
            if rendering.deployment_checks is not None
            for issue in rendering.deployment_checks(site)
        )


@dataclass(frozen=True, slots=True)
class Operations:
    """Every health check, scheduled job and lifecycle step in this process."""

    health_checks: tuple[HealthCheck, ...]
    #: By name, because the worker resolves a job from a name that arrived in a
    #: queue message.
    scheduled_jobs: Mapping[str, ScheduledJob]
    operations: tuple[FeatureOperations, ...]

    def startup(self) -> None:
        """Let every feature do what it owes a starting node.

        Promises no order: a startup that depends on another feature's belongs
        in the composition root, where the build order is explicit.
        """
        for item in self.operations:
            if item.startup is not None:
                item.startup()

    def shutdown(self) -> None:
        """The mirror of :meth:`startup`, in reverse order."""
        for item in reversed(self.operations):
            if item.shutdown is not None:
                item.shutdown()


def _unique_checks(checks: Sequence[HealthCheck]) -> tuple[HealthCheck, ...]:
    """Refuse two checks that answer to one name.

    `/health` reports checks by name, so a duplicate would leave an operator
    unable to tell which feature's check is failing.
    """
    seen: dict[str, str] = {}
    for check in checks:
        if check.name in seen:
            raise FeatureError(
                f"features {seen[check.name]!r} and {check.feature!r} both "
                f"contribute a health check named {check.name!r}"
            )
        seen[check.name] = check.feature
    return tuple(checks)


def _unique_jobs(jobs: Sequence[ScheduledJob]) -> dict[str, ScheduledJob]:
    """Every job by name, refusing two that would resolve to one."""
    table: dict[str, ScheduledJob] = {}
    for job in jobs:
        if job.name in table:
            raise FeatureError(
                f"features {table[job.name].feature!r} and {job.feature!r} both "
                f"contribute a job named {job.name!r}"
            )
        table[job.name] = job
    return table


__all__ = ["FeatureRegistry", "Operations", "Rendering", "merge_variables"]
