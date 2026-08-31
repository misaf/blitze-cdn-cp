"""Ownership and composition tests for site-serving policy."""

import pytest
from pydantic import ValidationError

from blitzecdn.features.sites.domain import CdnSite, SitePolicy
from blitzecdn.features.sites.plugin import blitzecdn_fleet_desired_state
from blitzecdn.features.sites.policy import (
    CacheQueryStringMode,
    CompressionMode,
    SiteFirewall,
    SiteVisitorHeaders,
    SslMode,
    managed_certificate_paths,
)


def test_policy_concepts_are_owned_by_cohesive_site_modules():
    assert CompressionMode.__module__.endswith(".sites.policy.compression")
    assert CacheQueryStringMode.__module__.endswith(".sites.policy.cache")
    assert SslMode.__module__.endswith(".sites.policy.tls")
    assert SiteFirewall.__module__.endswith(".sites.policy.security")
    assert SiteVisitorHeaders.__module__.endswith(".sites.policy.headers")
    assert [mode.value for mode in CompressionMode] == ["off", "gzip", "brotli"]


def test_composed_site_policy_keeps_the_flat_persisted_contract():
    document = SitePolicy().model_dump(mode="json")

    assert document["compression"] == "brotli"
    assert document["cache_query_string_mode"] == "include"
    assert document["visitor_headers"] == {
        "connecting_ip": True,
        "ip_country": False,
    }
    assert (
        not {
            "tls",
            "protocols",
            "cache",
            "security",
            "headers",
            "origin",
        }
        & document.keys()
    )


def test_cross_policy_http3_validation_stays_on_site_policy():
    with pytest.raises(ValidationError, match="http3_enabled=True requires ssl_mode"):
        SitePolicy(http3_enabled=True)

    assert SitePolicy(http3_enabled=True, ssl_mode=SslMode.FLEXIBLE).http3_enabled


def test_geoip_requirement_combines_security_and_header_policy():
    assert SitePolicy(firewall={"denied_countries": ["DE"]}).requires_geoip
    assert SitePolicy(visitor_headers={"ip_country": True}).requires_geoip
    assert not SitePolicy().requires_geoip


def test_sites_plugin_projects_http3_fleet_requirement_and_one_owner():
    def site(name: str, *, enabled: bool = True) -> CdnSite:
        chain, key = managed_certificate_paths(name)
        return CdnSite(
            name=name,
            server_names=(f"{name}.example.com",),
            origin_host="198.51.100.10",
            enabled=enabled,
            ssl_mode="full",
            certificate_mode="requested",
            certificate_path=chain,
            certificate_key_path=key,
            http3_enabled=True,
        )

    contribution = blitzecdn_fleet_desired_state(
        (site("bravo"), site("alpha"), site("disabled", enabled=False)), object()
    )

    assert contribution.plugin == "sites"
    assert contribution.variables == {
        "blitzecdn_edge_http3_enabled": True,
        "blitzecdn_nginx_http3_listener_owner": "alpha",
    }
