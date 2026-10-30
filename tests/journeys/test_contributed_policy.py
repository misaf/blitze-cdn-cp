"""A site setting contributed by one feature reaches every surface (#37).

The feature below exists only in this test. Registered beside the built-in
features, its contract must be stored, patched, overridden by a rule, served
over HTTP, published in the API schema, rendered for the edge and asked about
by capability validation, without one edit to `dns`, `serving` or core.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest
from fastapi.testclient import TestClient
from pydantic import ConfigDict, Field, ValidationError
from support.control_plane import API_HEADERS, seed_site_over_http

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.dns.domain import DomainPatch, Rule
from blitzecdn.capabilities.serving.contract import SitePolicy
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.composition import compose_registry, load_registry
from blitzecdn.core.domain.policy import CapabilityPolicy
from blitzecdn.core.features import (
    Feature,
    FeatureMetadata,
    FeatureRegistry,
    PolicyContribution,
    PolicyView,
)
from blitzecdn.entrypoints.api import create_app


class RateLimitPolicy(CapabilityPolicy):
    """Requests per second a visitor may make to one site; 0 is unlimited."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rate_limit: int = Field(default=0, ge=0, description="Requests per second.")

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        return {"edge_rate_limits": ("rate_limit",)} if self.rate_limit else {}


def _no_limit_above_ten_thousand(policy: PolicyView) -> None:
    if policy[RateLimitPolicy].rate_limit > 10_000:
        raise ValueError("rate_limit above 10000 is not a limit")


def _feature(*, provides: bool) -> Feature:
    return Feature(
        metadata=FeatureMetadata(
            name="rate_limiting",
            version="1",
            # A feature always answers for its own name, so the token differs.
            provides=frozenset({"edge_rate_limits"}) if provides else frozenset(),
        ),
        site_policy=(
            PolicyContribution(
                feature="rate_limiting",
                group="rate_limiting",
                contract=RateLimitPolicy,
                checks=(_no_limit_above_ten_thousand,),
            ),
        ),
    )


@pytest.fixture
def registry() -> Iterator[FeatureRegistry]:
    """The built-in features plus rate limiting; the real policy is restored after."""
    saved = SitePolicy._schema
    builtin = load_registry().features
    yield compose_registry((*builtin, _feature(provides=True)))
    SitePolicy._schema = saved


def test_the_policy_stores_validates_and_requests_the_contributed_setting(registry):
    policy = SitePolicy.from_settings({"rate_limit": 50})

    assert policy[RateLimitPolicy].rate_limit == 50
    assert policy.model_dump(mode="json")["rate_limiting"] == {"rate_limit": 50}
    assert SitePolicy.model_validate_json(policy.model_dump_json()) == policy
    assert policy.capability_requirements["edge_rate_limits"] == ("rate_limit",)
    assert registry.missing(policy.required_capabilities) == ()
    # The contract's own constraint, and the check contributed beside it.
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        SitePolicy.from_settings({"rate_limit": -1})
    with pytest.raises(ValidationError, match="not a limit"):
        SitePolicy.from_settings({"rate_limit": 20_000})


def test_a_zone_patch_and_a_rule_override_accept_the_setting(registry):
    patch = DomainPatch.model_validate({"rate_limit": 5})
    assert patch.model_dump(exclude_unset=True) == {"rate_limit": 5}

    rule = Rule.model_validate(
        {
            "domain": "example.com",
            "name": "api",
            "match": "api.example.com",
            "overrides": {"rate_limit": 5},
        }
    )
    assert rule.overrides == {"rate_limit": 5}
    with pytest.raises(ValidationError, match="can only override zone settings"):
        Rule.model_validate(
            {"domain": "example.com", "name": "x", "overrides": {"rate_limits": 5}}
        )


def test_the_edge_document_carries_the_setting(registry):
    site = CdnSite(
        name="example-com",
        server_names=("cdn.example.com",),
        origin_host="origin.example.com",
        policy=SitePolicy.from_settings({"rate_limit": 7}),
    )

    assert site_to_ansible(site)["rate_limit"] == 7


def test_the_http_api_serves_patches_and_publishes_the_setting(registry, settings):
    with TestClient(create_app(settings, features=registry)) as client:
        created = client.post(
            "/v3/domains",
            json={"name": "example.com", "rate_limit": 10},
            headers=API_HEADERS,
        )
        assert created.status_code == 201, created.text
        assert created.json()["rate_limit"] == 10

        patched = client.patch(
            "/v3/domains/example.com", json={"rate_limit": 20}, headers=API_HEADERS
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["rate_limit"] == 20

        refused = client.patch(
            "/v3/domains/example.com", json={"rate_limit": -1}, headers=API_HEADERS
        )
        assert refused.status_code == 422

        seed_site_over_http(client, rate_limit=30)
        hosts = client.get("/v3/hosts", headers=API_HEADERS).json()["items"]
        assert {host["rate_limit"] for host in hosts} == {30}

        schemas = client.get("/openapi.json").json()["components"]["schemas"]
        for name in ("Domain", "DomainPatch", "CdnSite"):
            assert "rate_limit" in schemas[name]["properties"], name


def test_a_site_asking_for_an_absent_capability_is_refused_by_name():
    saved = SitePolicy._schema
    try:
        builtin = load_registry().features
        registry = compose_registry((*builtin, _feature(provides=False)))
        policy = SitePolicy.from_settings({"rate_limit": 5})

        assert registry.missing(policy.required_capabilities) == ("edge_rate_limits",)
    finally:
        SitePolicy._schema = saved
