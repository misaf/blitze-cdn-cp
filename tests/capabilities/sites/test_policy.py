"""Composition tests for the site model: what it owns, and what it borrows."""

import pytest
from pydantic import ValidationError

from blitzecdn.capabilities.cache.policy import CacheQueryStringMode
from blitzecdn.capabilities.compression.policy import CompressionMode
from blitzecdn.capabilities.security.policy import SiteFirewall
from blitzecdn.capabilities.sites.domain import CdnSite, SitePolicy
from blitzecdn.capabilities.sites.policy import SiteVisitorHeaders
from blitzecdn.capabilities.tls.policy import SslMode


def test_every_policy_concept_is_defined_by_the_capability_that_owns_it():
    """The ownership rule, asserted on the values themselves.

    ``__module__`` is where a class was defined, not where it was imported
    from, so this fails if a capability's contract is redefined or aliased into
    ``sites`` rather than composed from its owner.
    """
    assert CompressionMode.__module__ == "blitzecdn.capabilities.compression.policy"
    assert SslMode.__module__ == "blitzecdn.capabilities.tls.policy"
    assert SiteFirewall.__module__ == "blitzecdn.capabilities.security.policy"
    assert CacheQueryStringMode.__module__ == "blitzecdn.capabilities.cache.policy"
    # The one that stays: no distribution could carry the ``BZ-*`` headers away,
    # so there is no capability to reunite this contract with.
    assert (
        SiteVisitorHeaders.__module__ == "blitzecdn.capabilities.sites.policy.headers"
    )
    assert [mode.value for mode in CompressionMode] == ["off", "gzip", "brotli"]


def test_composed_site_policy_keeps_the_flat_persisted_contract():
    """Composition changed owners, not the shape anything downstream reads."""
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
            "compression_policy",
        }
        & document.keys()
    )


def test_cross_capability_rules_stay_on_the_site_that_composes_them():
    """HTTP/3 needing edge TLS reads two capabilities, so neither can own it."""
    with pytest.raises(ValidationError, match="http3_enabled=True requires ssl_mode"):
        SitePolicy(http3_enabled=True)

    assert SitePolicy(http3_enabled=True, ssl_mode=SslMode.FLEXIBLE).http3_enabled


def test_geoip_requirement_combines_security_and_header_policy():
    assert SitePolicy(firewall={"denied_countries": ["DE"]}).requires_geoip
    assert SitePolicy(visitor_headers={"ip_country": True}).requires_geoip
    assert not SitePolicy().requires_geoip


def test_site_package_re_exports_only_what_sites_owns():
    """Importing another capability's contract from `sites` must not work."""
    import blitzecdn.capabilities.sites as sites

    assert set(sites.__all__) == {
        "CdnSite",
        "HeaderPolicy",
        "OriginPolicy",
        "SitePatch",
        "SitePolicy",
        "SiteService",
        "SiteVisitorHeaders",
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
        assert not hasattr(sites, borrowed), borrowed
    assert issubclass(CdnSite, SitePolicy)
