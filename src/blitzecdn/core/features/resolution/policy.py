"""The contracts a site policy is composed of, each group and setting owned once.

A site's policy is stored nested by group and edited flat by setting name, so
both have to be unambiguous: a group or a setting with two owners would make a
stored or edited value land in whichever contract was composed last.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from blitzecdn.core.domain.policy import CapabilityPolicy
from blitzecdn.core.exceptions import FeatureError
from blitzecdn.core.features.types import PolicyCheck, PolicyContribution, SiteCheck

__all__ = ["RESERVED_POLICY_NAMES", "ResolvedPolicySchema", "resolve_policy_schema"]

#: Names the site policy keeps for itself, as a group and as a flat setting.
RESERVED_POLICY_NAMES = frozenset({"enabled"})


@dataclass(frozen=True, slots=True)
class ResolvedPolicySchema:
    """Every contributed contract by group, and which group owns each setting."""

    #: In feature-name order, so the same features always compose one policy.
    groups: Mapping[str, type[CapabilityPolicy]]
    #: Flat setting name to the group whose contract declares it.
    setting_owners: Mapping[str, str]
    checks: tuple[PolicyCheck, ...]
    site_checks: tuple[SiteCheck, ...]

    def group_of(self, contract: type[CapabilityPolicy]) -> str:
        """The group a contract is stored under."""
        for group, registered in self.groups.items():
            if registered is contract:
                return group
        raise FeatureError(
            f"{contract.__name__} is not part of the site policy; the feature "
            "that owns it has to contribute it as a `PolicyContribution`"
        )


def resolve_policy_schema(
    contributions: Iterable[PolicyContribution],
) -> ResolvedPolicySchema:
    """Validate ownership and dependencies, then order contracts by feature."""
    ordered = sorted(contributions, key=lambda item: (item.feature, item.group))
    groups: dict[str, PolicyContribution] = {}
    contracts: dict[type[CapabilityPolicy], PolicyContribution] = {}
    setting_owners: dict[str, str] = {}
    for item in ordered:
        if not isinstance(item.contract, type) or not issubclass(
            item.contract, CapabilityPolicy
        ):
            raise FeatureError(
                f"feature {item.feature!r} contributes policy group {item.group!r} "
                "with a contract that is not a CapabilityPolicy"
            )
        if not item.group.isidentifier() or item.group in RESERVED_POLICY_NAMES:
            raise FeatureError(
                f"feature {item.feature!r} contributes invalid policy group "
                f"{item.group!r}; a group is an identifier other than "
                + ", ".join(sorted(RESERVED_POLICY_NAMES))
            )
        if item.group in groups:
            raise FeatureError(
                f"policy group {item.group!r} is contributed by both feature "
                f"{groups[item.group].feature!r} and feature {item.feature!r}"
            )
        if item.contract in contracts:
            raise FeatureError(
                f"{item.contract.__name__} is contributed as policy group "
                f"{contracts[item.contract].group!r} and as {item.group!r}; a "
                "contract has one place in the policy"
            )
        for name in item.contract.model_fields:
            if name in RESERVED_POLICY_NAMES:
                raise FeatureError(
                    f"feature {item.feature!r} declares policy setting {name!r}, "
                    "which the site policy keeps for itself"
                )
            if name in setting_owners:
                raise FeatureError(
                    f"policy setting {name!r} is declared by both group "
                    f"{setting_owners[name]!r} and group {item.group!r}"
                )
            setting_owners[name] = item.group
        groups[item.group] = item
        contracts[item.contract] = item
    for item in ordered:
        for needed in item.depends_on:
            if needed not in contracts:
                raise FeatureError(
                    f"policy group {item.group!r} of feature {item.feature!r} "
                    f"depends on {needed.__name__}, which no feature contributes"
                )
    return ResolvedPolicySchema(
        groups={group: item.contract for group, item in groups.items()},
        setting_owners=setting_owners,
        checks=tuple(check for item in ordered for check in item.checks),
        site_checks=tuple(check for item in ordered for check in item.site_checks),
    )
