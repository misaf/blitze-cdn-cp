"""Composition tests for the site model: what it owns, and what it borrows."""

import pytest
from pydantic import ValidationError
from support.composition import composition_state, temporary_policy_composition

from blitzecdn.capabilities.cache.contract import CachePolicy, CacheQueryStringMode
from blitzecdn.capabilities.compression.contract import CompressionMode
from blitzecdn.capabilities.http.contract import (
    HeaderPolicy,
    MaxUploadSize,
    ProtocolPolicy,
    SiteVisitorHeaders,
)
from blitzecdn.capabilities.origins.contract import OriginPolicy
from blitzecdn.capabilities.security.contract import SiteFirewall
from blitzecdn.capabilities.tls.contract import (
    SslMode,
    TlsPolicy,
    http3_requires_edge_tls,
)
from blitzecdn.composition import BUILTIN_REGISTRY
from blitzecdn.core.domain.policy import CapabilityPolicy
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.domain.site_policy import SitePolicy
from blitzecdn.core.exceptions import FeatureError
from blitzecdn.core.features import PolicyContribution


def test_every_policy_concept_is_defined_by_the_capability_that_owns_it():
    """The ownership rule, asserted on the values themselves.

    ``__module__`` is where a class was defined, so this fails if a contract is
    redefined or aliased into ``sites``.
    """
    assert CdnSite.__module__ == "blitzecdn.core.domain.site"
    assert SitePolicy.__module__ == "blitzecdn.core.domain.site_policy"
    assert OriginPolicy.__module__ == "blitzecdn.capabilities.origins.contract"
    assert CompressionMode.__module__ == "blitzecdn.capabilities.compression.contract"
    assert SslMode.__module__ == "blitzecdn.capabilities.tls.contract"
    assert SiteFirewall.__module__ == "blitzecdn.capabilities.security.contract"
    assert CacheQueryStringMode.__module__ == "blitzecdn.capabilities.cache.contract"
    assert SiteVisitorHeaders.__module__ == "blitzecdn.capabilities.http.contract"
    assert [mode.value for mode in CompressionMode] == ["off", "gzip", "brotli"]


def test_composed_policy_persists_nested_values_and_projects_flat_settings():
    policy = SitePolicy()
    document = policy.model_dump(mode="json")
    assert document["compression"] == {"compression": "brotli"}
    assert document["cache"]["cache_query_string_mode"] == "include"
    assert document["headers"]["visitor_headers"] == {
        "connecting_ip": True,
        "ip_country": False,
    }
    assert set(document) == {
        "enabled",
        "tls",
        "protocols",
        "origin",
        "cache",
        "compression",
        "error_pages",
        "security",
        "headers",
    }
    settings = policy.settings(mode="json")
    assert settings["compression"] == "brotli"
    assert settings["cache_query_string_mode"] == "include"
    assert SitePolicy.from_settings(settings) == policy


def test_max_upload_size_is_a_closed_set_the_http_capability_owns():
    """A tier the enum does not name is refused, not passed through to nginx."""
    assert MaxUploadSize.__module__ == "blitzecdn.capabilities.http.contract"
    assert [tier.value for tier in MaxUploadSize] == ["100m", "200m"]

    assert SitePolicy().protocols.max_upload_size is MaxUploadSize.SMALL
    assert (
        SitePolicy().model_dump(mode="json")["protocols"]["max_upload_size"] == "100m"
    )
    assert (
        SitePolicy.from_settings({"max_upload_size": "200m"}).protocols.max_upload_size
        is MaxUploadSize.LARGE
    )

    for refused in ("50m", "200mb", "100M", "", "0"):
        with pytest.raises(ValidationError):
            SitePolicy.from_settings({"max_upload_size": refused})


def test_the_upload_limit_asks_for_no_detachable_capability():
    """Core nginx always has the directive, so neither tier requests a wheel."""
    for tier in MaxUploadSize:
        requested = SitePolicy.from_settings(
            {
                "cache_enabled": False,
                "compression": CompressionMode.OFF,
                "max_upload_size": tier,
            }
        ).required_capabilities
        assert requested == frozenset()


def test_a_rule_across_contracts_is_contributed_by_the_capability_that_owns_it():
    """HTTP/3 needing edge TLS is TLS's rule, declared against what it reads."""
    (tls,) = (
        item
        for item in BUILTIN_REGISTRY.policy_contributions()
        if item.contract is TlsPolicy
    )
    assert http3_requires_edge_tls in tls.checks
    assert ProtocolPolicy in tls.depends_on

    with pytest.raises(ValidationError, match="http3_enabled=True requires ssl_mode"):
        SitePolicy.from_settings({"http3_enabled": True})

    assert SitePolicy.from_settings(
        {
            "http3_enabled": True,
            "ssl_mode": SslMode.FLEXIBLE,
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/site.pem",
            "certificate_key_path": "/etc/ssl/site.key",
        }
    )[ProtocolPolicy].http3_enabled


def test_geoip_requirement_combines_security_and_header_policy():
    assert (
        "geoip"
        in SitePolicy.from_settings(
            {"firewall": {"denied_countries": ["DE"]}}
        ).required_capabilities
    )
    assert (
        "geoip"
        in SitePolicy.from_settings(
            {"visitor_headers": {"ip_country": True}}
        ).required_capabilities
    )
    assert "geoip" not in SitePolicy().required_capabilities


def test_the_policy_package_re_exports_only_what_it_owns():
    """Core composes policy without importing concrete capability contracts."""
    import blitzecdn.core.domain.site_policy as policy

    assert set(policy.__all__) == {
        "SitePolicy",
    }
    borrowed_names = (
        "HeaderPolicy",
        "OriginPolicy",
        "SiteVisitorHeaders",
        "CompressionMode",
        "SslMode",
        "SiteFirewall",
        "HttpScheme",
        "CachePolicy",
        "CacheQueryStringMode",
    )
    for borrowed in borrowed_names:
        assert not hasattr(policy, borrowed), borrowed
    assert not issubclass(CdnSite, SitePolicy)
    assert CdnSite.model_fields["policy"].annotation is SitePolicy


def test_domain_values_accept_only_nested_policy_storage():
    from blitzecdn.capabilities.sites.domain import Domain

    policy = SitePolicy(cache={"cache_enabled": False})
    zone = Domain(name="example.com", policy=policy)
    site = CdnSite(name="example-com", origin_host="198.51.100.10", policy=policy)
    assert not issubclass(Domain, SitePolicy)
    assert not issubclass(CdnSite, SitePolicy)
    assert zone.model_dump(mode="json")["policy"]["cache"]["cache_enabled"] is False
    assert site.policy == zone.policy
    assert Domain.model_validate_json(zone.model_dump_json()) == zone
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Domain(name="example.com", cache_enabled=False)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SitePolicy(cache_enabled=False)
    with pytest.raises(ValueError, match="unknown serving settings"):
        SitePolicy.from_settings({"cache_enabeld": False})


def test_named_settings_preserve_unrelated_policy_and_replace_leaf_blocks():
    initial = SitePolicy(
        cache={"cache_enabled": False, "cache_valid_success": "1h"},
        origin={"origin_sni": "origin.example.com"},
        security={"firewall": {"denied_methods": ("DELETE",)}},
        headers={"visitor_headers": {"connecting_ip": False, "ip_country": True}},
    )
    changed = initial.applying({"cache_valid_success": "5m", "visitor_headers": {}})
    assert not changed.cache.cache_enabled
    assert changed.cache.cache_valid_success == "5m"
    assert changed.origin == initial.origin
    assert changed.security == initial.security
    assert changed.headers.visitor_headers == SiteVisitorHeaders()
    assert initial.cache.cache_valid_success == "1h"
    with pytest.raises(ValidationError, match="frozen_instance"):
        changed.cache.cache_enabled = True
    with pytest.raises(ValidationError, match="http3_enabled=True requires ssl_mode"):
        changed.applying({"http3_enabled": True})


class _FeaturePolicy(CapabilityPolicy):
    feature_enabled: bool = True

    @property
    def capability_requirements(self):
        return {"feature": ("feature_enabled",)} if self.feature_enabled else {}


def test_a_contributed_contract_joins_every_site_policy(policy_composition):
    policy_composition.contributions(
        PolicyContribution(feature="toy", group="feature", contract=_FeaturePolicy)
    )

    policy = SitePolicy.from_settings({"feature_enabled": True})
    assert policy[_FeaturePolicy].feature_enabled
    assert policy.capability_requirements["feature"] == ("feature_enabled",)
    assert (
        "feature"
        not in policy.applying({"feature_enabled": False}).required_capabilities
    )
    assert SitePolicy.model_validate_json(policy.model_dump_json()) == policy
    site = CdnSite(name="toy", origin_host="origin.example.com", policy=policy)
    assert CdnSite.model_validate_json(site.model_dump_json()) == site


def test_a_flat_setting_cannot_be_owned_by_two_contracts(policy_composition):
    class SecondCache(CachePolicy):
        pass

    with pytest.raises(FeatureError, match=r"'cache_enabled'.*'cache'.*'second'"):
        policy_composition.contributions(
            PolicyContribution(feature="toy", group="second", contract=SecondCache)
        )


def test_a_policy_cannot_be_validated_before_it_is_composed(policy_composition):
    policy_composition.uncompose()

    with pytest.raises(FeatureError, match="not composed yet"):
        SitePolicy()
    with pytest.raises(FeatureError, match="not composed yet"):
        CdnSite(name="early", origin_host="origin.example.com")


def test_composing_the_same_contributions_again_keeps_the_model(policy_composition):
    composed = SitePolicy.composed()

    policy_composition.features()

    assert SitePolicy.composed() is composed


def test_features_contribute_only_the_policy_they_own():
    contributions = {
        item.contract: item for item in BUILTIN_REGISTRY.policy_contributions()
    }
    assert contributions[HeaderPolicy].feature == "http"
    assert contributions[HeaderPolicy].group == "headers"
    assert contributions[OriginPolicy].feature == "origins"
    assert contributions[OriginPolicy].group == "origin"
    assert all(item.feature != "sites" for item in contributions.values())


def test_a_site_can_use_a_policy_without_http_tls_or_origin_contracts(
    policy_composition,
):
    # Core identity and policy remain usable with an unrelated contribution.
    policy_composition.contributions(
        PolicyContribution(feature="toy", group="feature", contract=_FeaturePolicy),
        builtin=False,
    )
    site = CdnSite(name="toy", origin_host="origin.example.com")
    assert site.policy[_FeaturePolicy].feature_enabled
    assert site.policy.required_capabilities == frozenset({"feature"})
    assert CdnSite.model_validate_json(site.model_dump_json()) == site


# --- temporary composition -------------------------------------------------


def _toy() -> PolicyContribution:
    return PolicyContribution(feature="toy", group="feature", contract=_FeaturePolicy)


def _assert_builtin_restored(before) -> None:
    assert composition_state() == before
    assert "feature" not in SitePolicy.component_types()
    with pytest.raises(ValueError, match="unknown serving settings: feature_enabled"):
        SitePolicy.from_settings({"feature_enabled": True})


def test_a_temporary_composition_restores_the_builtin_policy_after_it_passes():
    builtin = SitePolicy.composed()
    before = composition_state()

    with temporary_policy_composition() as composer:
        composer.contributions(_toy())
        assert SitePolicy.from_settings({"feature_enabled": False})
        # Every model nesting the policy grows a model for the temporary schema.
        CdnSite(name="toy", origin_host="origin.example.com")

    _assert_builtin_restored(before)
    assert SitePolicy.composed() is builtin


def test_a_temporary_composition_restores_the_policy_after_a_failed_assertion():
    before = composition_state()

    with (
        pytest.raises(AssertionError, match="deliberate"),
        temporary_policy_composition() as composer,
    ):
        composer.features()
        composer.contributions(_toy())
        raise AssertionError("deliberate")

    _assert_builtin_restored(before)


def test_a_temporary_composition_restores_the_policy_when_composing_raises():
    before = composition_state()

    class SecondCache(CachePolicy):
        pass

    with (
        pytest.raises(FeatureError, match="cache_enabled"),
        temporary_policy_composition() as composer,
    ):
        composer.contributions(_toy())
        composer.contributions(
            PolicyContribution(feature="toy", group="second", contract=SecondCache)
        )

    _assert_builtin_restored(before)


def test_an_uncomposed_policy_is_composed_again_afterwards():
    before = composition_state()

    with temporary_policy_composition() as composer:
        composer.uncompose()
        with pytest.raises(FeatureError, match="not composed yet"):
            SitePolicy()

    _assert_builtin_restored(before)
    assert SitePolicy().enabled


def test_sequential_temporary_compositions_do_not_see_each_others_contributions():
    class OtherPolicy(CapabilityPolicy):
        other_enabled: bool = False

    before = composition_state()
    with temporary_policy_composition() as composer:
        composer.contributions(_toy())
    with temporary_policy_composition() as composer:
        composer.contributions(
            PolicyContribution(feature="other", group="other", contract=OtherPolicy)
        )
        assert set(SitePolicy.component_types()) >= {"other"}
        assert "feature" not in SitePolicy.component_types()

    _assert_builtin_restored(before)
