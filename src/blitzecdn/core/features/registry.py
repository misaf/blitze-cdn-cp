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
from typing import TYPE_CHECKING

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
    ScheduledJob,
    SiteStateContribution,
    StateValue,
    ValidationIssue,
    ValidationResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from fastapi import APIRouter

    from blitzecdn.capabilities.serving.domain import CdnSite


def merge_variables(
    contributions: Sequence[SiteStateContribution] | Sequence[FleetStateContribution],
    *,
    subject: str,
) -> dict[str, StateValue]:
    """Merge every feature's variables into one document, order-independently.

    Two features writing one variable is a conflict, not a race won by whichever
    registered last. The single exception is a declared override: a feature may
    say it is deliberately replacing a value another produced — ``certificates``
    replaces the certificate paths projected from the site model, because the
    installed certificate lives on this controller and the model only knows
    where it will land on the edge. Exactly one feature may claim that per
    variable, and claiming a variable nobody else writes is fine: whether the
    site model happens to carry a path is not something the certificate feature
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
        """Every capability token the installed features answer for.

        The union of what each feature declared, and the only thing anything in
        core asks about "is that capability here?". Core never learns which
        distribution supplied a token, which is what keeps
        `if compression_installed:` from being writable in the first place.
        """
        return frozenset().union(*(feature.capabilities for feature in self.metadata))

    def missing(self, required: Iterable[str]) -> tuple[str, ...]:
        """Which of ``required`` no installed feature provides, sorted."""
        return tuple(sorted(set(required) - self.capabilities))

    def require(self, required: Iterable[str], *, subject: str) -> None:
        """Refuse to continue while a named capability is not installed.

        Deterministic and generic: the tokens come from configuration, the
        answer comes from feature metadata, and nothing between them names a
        capability. A configuration that asks for a capability no installed
        feature provides is a startup failure with the token in it, not a
        control plane that comes up and quietly serves its absence.
        """
        absent = self.missing(required)
        if absent:
            installed = ", ".join(sorted(self.capabilities)) or "none"
            raise FeatureError(
                f"{subject} requires the "
                f"{'capabilities' if len(absent) > 1 else 'capability'} "
                f"{', '.join(absent)}, which no installed feature provides. "
                f"Installed capabilities: {installed}. Install the distribution "
                "that supplies it, or remove it from `required_capabilities`."
            )

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
        """Every installed feature's Ansible roles directory, in a fixed order.

        Registration order, like the routers and the commands. The search path
        this feeds is composed in :mod:`blitzecdn.core.features.resolution`,
        which is where determinism and shadowing are actually decided.
        """
        return tuple(item for feature in self.features for item in feature.ansible)

    def configuration_contributions(self) -> tuple[ConfigurationContribution, ...]:
        """Every installed feature's claim on `BLITZE_*` names it configures.

        Read once, before any adapter exists, because several adapters are
        built from what it resolves to. Ownership, collisions and unclaimed
        names are decided in :mod:`blitzecdn.core.features.resolution`.
        """
        return tuple(
            item for feature in self.features for item in feature.configuration
        )

    def nginx_contributions(self) -> tuple[NginxContribution, ...]:
        """Every installed package's static Nginx resources."""
        return tuple(item for feature in self.features for item in feature.nginx)

    # --- binding -----------------------------------------------------------

    def bind_rendering(self, renderings: Sequence[FeatureRendering]) -> Rendering:
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
        self, bound: Sequence[FeatureRendering] | Sequence[FeatureOperations], kind: str
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

    def _missing_capability_issues(self, site: CdnSite) -> tuple[ValidationIssue, ...]:
        requested = site.policy.capability_requirements
        return tuple(
            ValidationIssue(
                feature="capabilities",
                site=site.name,
                message=(
                    f"capability {capability!r} is not installed, and this "
                    f"site's {', '.join(requested[capability])} "
                    f"{'request' if len(requested[capability]) > 1 else 'requests'}"
                    " it; install a feature that provides it or disable the "
                    "site setting that requests it"
                ),
            )
            for capability in self.missing(requested)
        )


class Rendering:
    """Every feature's share of a release, asked per site or per fleet."""

    def __init__(
        self, registry: FeatureRegistry, renderings: tuple[FeatureRendering, ...]
    ) -> None:
        self._registry = registry
        self._renderings = renderings

    def site_variables(self, site: CdnSite) -> dict[str, StateValue]:
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

    def fleet_variables(self, sites: tuple[CdnSite, ...]) -> dict[str, StateValue]:
        contributions = [
            contribution
            for rendering in self._renderings
            if rendering.fleet_state is not None
            for contribution in [rendering.fleet_state(sites)]
            if contribution is not None
        ]
        return merge_variables(contributions, subject="fleet desired state")

    def validate_site(self, site: CdnSite) -> ValidationResult:
        """Every reason this site cannot be deployed by this installation.

        The capability issues come first and are generic: the tokens are the
        site's own `capability_requirements`, the answer is feature metadata,
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
        """What the *installed* features object to about this site.

        Separate from :meth:`validate_site` because the two answers have
        different consequences. A capability that is missing cannot be asked
        for anything and simply contributes nothing; a capability that is
        installed and objects must not then be asked to render this site,
        because the objection is usually the reason asking would fail.

        The release compiler makes the missing-capability check itself, against
        the edges as well as against this controller, so asking for both here
        would report each of them twice.
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

        Deliberately promises no order. A feature whose startup depends on
        another feature's startup has a dependency it has not declared and
        cannot declare here — that belongs in the composition root, where the
        two services are built in an order you can read.
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

    `/health` reports a check by `name`, so two features contributing
    `"database"` would put two entries under one label and an operator reading
    a failure would have no way to tell which wheel's check is red.
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
