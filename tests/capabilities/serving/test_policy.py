"""Composition tests for the site model: what it owns, and what it borrows."""

import pytest
from pydantic import ValidationError

from blitzecdn.capabilities.cache.policy import CacheQueryStringMode
from blitzecdn.capabilities.compression.policy import CompressionMode
from blitzecdn.capabilities.http.policy import MaxUploadSize
from blitzecdn.capabilities.security.policy import SiteFirewall
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.capabilities.serving.policy import SitePolicy, SiteVisitorHeaders
from blitzecdn.capabilities.tls.policy import SslMode


def test_every_policy_concept_is_defined_by_the_capability_that_owns_it():
    """The ownership rule, asserted on the values themselves.

    ``__module__`` is where a class was defined, not where it was imported
    from, so this fails if a capability's contract is redefined or aliased into
    ``dns`` rather than composed from its owner.
    """
    assert CompressionMode.__module__ == "blitzecdn.capabilities.compression.policy"
    assert SslMode.__module__ == "blitzecdn.capabilities.tls.policy"
    assert SiteFirewall.__module__ == "blitzecdn.capabilities.security.policy"
    assert CacheQueryStringMode.__module__ == "blitzecdn.capabilities.cache.policy"
    # The one that stays: no distribution could carry the ``BZ-*`` headers away,
    # so there is no capability to reunite this contract with.
    assert (
        SiteVisitorHeaders.__module__ == "blitzecdn.capabilities.serving.policy.headers"
    )
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
        "security",
        "headers",
    }
    settings = policy.settings(mode="json")
    assert settings["compression"] == "brotli"
    assert settings["cache_query_string_mode"] == "include"
    assert SitePolicy.from_settings(settings) == policy


def test_max_upload_size_is_a_closed_set_the_http_capability_owns():
    """A tier the enum does not name is refused, not passed through to nginx.

    The point of the enum over a size string: nothing between here and the
    rendered directive has to know what a valid nginx size looks like.
    """
    assert MaxUploadSize.__module__ == "blitzecdn.capabilities.http.policy"
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


def test_cross_capability_rules_stay_on_the_site_that_composes_them():
    """HTTP/3 needing edge TLS reads two capabilities, so neither can own it."""
    with pytest.raises(ValidationError, match="http3_enabled=True requires ssl_mode"):
        SitePolicy.from_settings({"http3_enabled": True})

    assert SitePolicy.from_settings(
        {"http3_enabled": True, "ssl_mode": SslMode.FLEXIBLE}
    ).protocols.http3_enabled


def test_geoip_requirement_combines_security_and_header_policy():
    assert SitePolicy.from_settings(
        {"firewall": {"denied_countries": ["DE"]}}
    ).requires_geoip
    assert SitePolicy.from_settings(
        {"visitor_headers": {"ip_country": True}}
    ).requires_geoip
    assert not SitePolicy().requires_geoip


def test_the_policy_package_re_exports_only_what_it_owns():
    """Importing another capability's contract from `serving.policy` must not work."""
    import blitzecdn.capabilities.serving.policy as policy

    assert set(policy.__all__) == {
        "HeaderPolicy",
        "OriginPolicy",
        "SiteVisitorHeaders",
        "SitePolicy",
    }
    borrowed_names = (
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
    from blitzecdn.capabilities.dns.domain import Domain

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


def test_nested_capability_values_participate_without_inheritance_discovery():
    from pydantic import Field

    from blitzecdn.core.domain.policy import CapabilityPolicy

    class FeaturePolicy(CapabilityPolicy):
        feature_enabled: bool = True

        @property
        def capability_requirements(self):
            return {"feature": ("feature_enabled",)} if self.feature_enabled else {}

    class ExtendedPolicy(SitePolicy):
        feature: FeaturePolicy = Field(default_factory=FeaturePolicy)

    policy = ExtendedPolicy.from_settings({"feature_enabled": True})
    assert policy.feature.feature_enabled
    assert policy.capability_requirements["feature"] == ("feature_enabled",)
    assert (
        "feature"
        not in policy.applying({"feature_enabled": False}).required_capabilities
    )
    assert ExtendedPolicy.model_validate_json(policy.model_dump_json()) == policy


def test_a_flat_setting_cannot_be_owned_by_two_nested_contracts():
    from pydantic import Field

    from blitzecdn.capabilities.cache.policy import CachePolicy

    class AmbiguousPolicy(SitePolicy):
        second_cache: CachePolicy = Field(default_factory=CachePolicy)

    with pytest.raises(TypeError, match=r"cache_enabled.*multiple owners"):
        AmbiguousPolicy()
