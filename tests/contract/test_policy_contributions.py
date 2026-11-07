"""The seam a site-policy contract crosses to join every site's policy."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from blitzecdn.core.domain.policy import CapabilityPolicy
from blitzecdn.core.exceptions import FeatureError
from blitzecdn.core.features import (
    Feature,
    FeatureMetadata,
    FeatureRegistry,
    PolicyContribution,
    PolicyView,
)
from blitzecdn.core.features.resolution import resolve_policy_schema


class Speed(CapabilityPolicy):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rate: int = 0


class Burst(CapabilityPolicy):
    model_config = ConfigDict(extra="forbid", frozen=True)

    burst: int = 0


class AlsoRate(CapabilityPolicy):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rate: int = 1


def _no_burst_without_rate(policy: PolicyView) -> None:
    if policy[Burst].burst and not policy[Speed].rate:
        raise ValueError("burst requires rate")


def test_contracts_are_keyed_by_group_and_ordered_by_feature():
    contributions = [
        PolicyContribution(feature="zulu", group="burst", contract=Burst),
        PolicyContribution(feature="alpha", group="speed", contract=Speed),
    ]

    first = resolve_policy_schema(contributions)
    second = resolve_policy_schema(reversed(contributions))

    assert first == second
    assert list(first.groups.items()) == [("speed", Speed), ("burst", Burst)]
    assert first.setting_owners == {"rate": "speed", "burst": "burst"}
    assert first.group_of(Burst) == "burst"


def test_checks_are_collected_in_feature_order():
    def first_check(policy: PolicyView) -> None:
        pass

    def site_check(site: str, policy: PolicyView) -> None:
        pass

    schema = resolve_policy_schema(
        [
            PolicyContribution(
                feature="zulu",
                group="burst",
                contract=Burst,
                depends_on=(Speed,),
                checks=(_no_burst_without_rate,),
                site_checks=(site_check,),
            ),
            PolicyContribution(
                feature="alpha", group="speed", contract=Speed, checks=(first_check,)
            ),
        ]
    )

    assert schema.checks == (first_check, _no_burst_without_rate)
    assert schema.site_checks == (site_check,)


def test_no_contributions_compose_an_empty_policy():
    schema = resolve_policy_schema([])

    assert dict(schema.groups) == {}
    assert schema.checks == schema.site_checks == ()


def test_two_features_cannot_own_one_group():
    with pytest.raises(FeatureError, match=r"'speed'.*'alpha'.*'zulu'"):
        resolve_policy_schema(
            [
                PolicyContribution(feature="zulu", group="speed", contract=Burst),
                PolicyContribution(feature="alpha", group="speed", contract=Speed),
            ]
        )


def test_a_contract_has_one_place_in_the_policy():
    with pytest.raises(FeatureError, match=r"Speed.*'fast'.*'speed'"):
        resolve_policy_schema(
            [
                PolicyContribution(feature="alpha", group="fast", contract=Speed),
                PolicyContribution(feature="zulu", group="speed", contract=Speed),
            ]
        )


def test_two_contracts_cannot_declare_one_setting():
    with pytest.raises(FeatureError, match=r"'rate'.*'also'.*'speed'"):
        resolve_policy_schema(
            [
                PolicyContribution(feature="alpha", group="also", contract=AlsoRate),
                PolicyContribution(feature="zulu", group="speed", contract=Speed),
            ]
        )


@pytest.mark.parametrize("group", ["enabled", "not-an-identifier", ""])
def test_a_group_has_to_be_an_identifier_the_policy_does_not_keep(group):
    with pytest.raises(FeatureError, match="invalid policy group"):
        resolve_policy_schema(
            [PolicyContribution(feature="alpha", group=group, contract=Speed)]
        )


def test_a_contract_cannot_declare_the_policy_enabled_switch():
    class Switch(CapabilityPolicy):
        enabled: bool = True

    with pytest.raises(FeatureError, match=r"'enabled'.*keeps for itself"):
        resolve_policy_schema(
            [PolicyContribution(feature="alpha", group="switch", contract=Switch)]
        )


def test_a_contract_has_to_be_a_capability_policy():
    class Plain(BaseModel):
        rate: int = 0

    with pytest.raises(FeatureError, match="not a CapabilityPolicy"):
        resolve_policy_schema(
            [
                PolicyContribution(
                    feature="alpha",
                    group="plain",
                    contract=Plain,  # type: ignore[arg-type]
                )
            ]
        )


def test_a_dependency_nobody_contributes_stops_startup():
    with pytest.raises(FeatureError, match=r"'burst'.*'zulu'.*Speed"):
        resolve_policy_schema(
            [
                PolicyContribution(
                    feature="zulu", group="burst", contract=Burst, depends_on=(Speed,)
                )
            ]
        )


def test_an_unregistered_contract_has_no_group():
    schema = resolve_policy_schema(
        [PolicyContribution(feature="alpha", group="speed", contract=Speed)]
    )

    with pytest.raises(FeatureError, match="Burst is not part of the site policy"):
        schema.group_of(Burst)


def test_the_registry_collects_policy_in_registration_order():
    speed = PolicyContribution(feature="zulu", group="speed", contract=Speed)
    burst = PolicyContribution(feature="alpha", group="burst", contract=Burst)
    registry = FeatureRegistry(
        [
            Feature(
                metadata=FeatureMetadata(name="zulu", version="1"), site_policy=(speed,)
            ),
            Feature(metadata=FeatureMetadata(name="quiet", version="1")),
            Feature(
                metadata=FeatureMetadata(name="alpha", version="1"),
                site_policy=(burst,),
            ),
        ]
    )

    assert registry.policy_contributions() == (speed, burst)
