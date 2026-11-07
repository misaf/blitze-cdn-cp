"""An origin is somewhere on the internet, never the edge or its own network.

Edges run nginx in the host's network namespace, so an origin of `127.0.0.1`
or `169.254.169.254` would hand visitors the edge's own services or its cloud
credentials.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from support.policy import site_from_settings

from blitzecdn.capabilities.dns.domain import (
    DnsRecord,
    Domain,
    derive_hosts,
    unservable_hosts,
)
from blitzecdn.capabilities.origins.contract import OriginPolicy
from blitzecdn.core.domain.site_policy import SitePolicy


@pytest.mark.parametrize(
    ("origin", "kind"),
    [
        ("127.0.0.1", "loopback"),
        ("127.8.9.10", "loopback"),
        ("::1", "loopback"),
        ("::ffff:127.0.0.1", "loopback"),
        ("169.254.169.254", "link-local"),
        ("::ffff:169.254.169.254", "link-local"),
        ("fe80::1", "link-local"),
        ("fd00:ec2::254", "cloud metadata"),
        ("100.100.100.200", "cloud metadata"),
        ("0.0.0.0", "unspecified"),  # noqa: S104 - an origin, not a bind
        ("0.1.2.3", "unspecified"),
        ("::", "unspecified"),
        ("224.0.0.1", "multicast"),
        ("ff02::1", "multicast"),
        ("240.0.0.1", "reserved"),
        ("255.255.255.255", "reserved"),
    ],
)
@pytest.mark.parametrize("opted_in", [False, True])
def test_an_origin_on_the_edge_itself_is_refused_even_with_the_opt_in(
    site_payload, origin, kind, opted_in
):
    site_payload |= {"origin_host": origin, "allow_private_origin": opted_in}

    with pytest.raises(ValidationError, match=f"is a {kind} address"):
        site_from_settings(site_payload)


@pytest.mark.parametrize(
    "origin",
    [
        "10.0.0.5",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.1",
        "100.64.0.1",
        "fd12:3456::1",
        "::ffff:10.0.0.5",
    ],
)
def test_a_private_origin_needs_the_sites_opt_in(site_payload, origin):
    site_payload["origin_host"] = origin

    with pytest.raises(ValidationError, match="allow_private_origin"):
        site_from_settings(site_payload)

    site = site_from_settings(site_payload | {"allow_private_origin": True})
    assert site.policy[OriginPolicy].allow_private_origin is True


@pytest.mark.parametrize(
    "origin",
    [
        "198.51.100.10",
        "203.0.113.7",
        "8.8.8.8",
        "172.32.0.1",
        "2001:db8::10",
        "2606:4700::1111",
        "origin.example.com",
    ],
)
def test_a_public_origin_needs_nothing(site_payload, origin):
    site_payload["origin_host"] = origin

    assert site_from_settings(site_payload).origin_host == origin


def test_the_opt_in_is_off_by_default():
    assert OriginPolicy().allow_private_origin is False


def test_a_proxied_record_at_the_metadata_service_is_reported_not_served():
    """Records are where origins come from; the zone keeps the rest serving."""
    zone = Domain(name="example.com")
    records = [
        DnsRecord(domain="example.com", name="www", value="198.51.100.10"),
        DnsRecord(domain="example.com", name="meta", value="169.254.169.254"),
    ]

    served = derive_hosts([zone], [], records)
    refused = unservable_hosts([zone], [], records)

    assert [site.origin_host for site in served] == ["198.51.100.10"]
    assert [host.hostnames for host in refused] == [("meta.example.com",)]
    assert "link-local" in refused[0].reason


def test_a_zone_that_opts_in_serves_its_private_origin():
    policy = SitePolicy().applying({"allow_private_origin": True})
    zone = Domain(name="example.com", policy=policy)
    records = [DnsRecord(domain="example.com", name="app", value="10.0.0.5")]

    (site,) = derive_hosts([zone], [], records)

    assert site.origin_host == "10.0.0.5"
    assert unservable_hosts([zone], [], records) == []
