"""Tests for certificate preflight.

Nothing here touches the network. ``_resolve`` and the CAA lookup are the two
seams that would, and both are replaced: the point of these tests is the
decision made about what was found, not the finding.
"""

from __future__ import annotations

from typing import ClassVar

import dns.exception
import dns.resolver
import pytest

from blitzecdn.domain.certificates import PreflightSeverity
from blitzecdn.domain.origins import OriginCheck
from blitzecdn.domain.sites import CdnSite, HttpScheme
from blitzecdn.infrastructure import preflight as preflight_module
from blitzecdn.infrastructure.preflight import (
    CertificatePreflight,
    _ancestors,
    _permitted_issuers,
)


class FakeOriginProbe:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok

    def check(self, site: CdnSite) -> OriginCheck:
        return OriginCheck(
            site=site.name,
            origin=f"{site.origin_host}:443",
            scheme=HttpScheme.HTTPS,
            resolved=True,
            reachable=self.ok,
            detail=None if self.ok else "no answer within 5s",
        )


@pytest.fixture
def site() -> CdnSite:
    return CdnSite(
        name="cdn-example-com",
        server_names=("cdn.example.com",),
        origin_host="198.51.100.10",
    )


@pytest.fixture
def build(settings, monkeypatch):
    """A preflight whose resolver and CAA lookup answer from a dict."""

    def make(
        *,
        addresses: dict[str, set[str]] | None = None,
        caa: tuple[str, list[tuple[str, str]]] | None = None,
        caa_error: Exception | None = None,
        edges: list[dict[str, str]] | None = None,
        origin_ok: bool = True,
    ) -> CertificatePreflight:
        resolved = addresses or {}
        monkeypatch.setattr(
            preflight_module,
            "_resolve",
            lambda host, resolver=None: resolved.get(host, set()),
        )

        def closest_caa(self, name):
            if caa_error is not None:
                raise caa_error
            return caa

        monkeypatch.setattr(CertificatePreflight, "_closest_caa", closest_caa)

        class FakeInventory:
            def list_edges(self):
                return edges if edges is not None else [{"host": "edge1.example.net"}]

        return CertificatePreflight(
            settings,
            inventory=FakeInventory(),  # type: ignore[arg-type]
            origin_probe=FakeOriginProbe(ok=origin_ok),  # type: ignore[arg-type]
        )

    return make


def _check(report, name):
    return next(check for check in report.checks if check.name == name)


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


def test_ancestors_walks_up_but_stops_before_the_tld():
    assert list(_ancestors("a.b.example.com")) == [
        "a.b.example.com",
        "b.example.com",
        "example.com",
    ]


def test_a_caa_rrset_with_no_relevant_tag_does_not_constrain_us():
    """An RRset of only iodef says nothing about who may issue."""
    assert _permitted_issuers([("iodef", "mailto:a@b.com")], wildcard=False) is None


def test_issue_semicolon_refuses_every_issuer():
    """Distinct from `None`: an explicit refusal must block, not fall through."""
    assert _permitted_issuers([("issue", ";")], wildcard=False) == set()


def test_issuer_parameters_are_not_part_of_the_identity():
    assert _permitted_issuers(
        [("issue", "letsencrypt.org; validationmethods=http-01")], wildcard=False
    ) == {"letsencrypt.org"}


def test_a_wildcard_falls_back_to_issue_when_no_issuewild_exists():
    tags = [("issue", "letsencrypt.org")]
    assert _permitted_issuers(tags, wildcard=True) == {"letsencrypt.org"}


def test_issuewild_overrides_issue_for_a_wildcard():
    tags = [("issue", "letsencrypt.org"), ("issuewild", "other-ca.example")]
    assert _permitted_issuers(tags, wildcard=True) == {"other-ca.example"}
    assert _permitted_issuers(tags, wildcard=False) == {"letsencrypt.org"}


# ----------------------------------------------------------------------
# Does DNS point at an edge?
# ----------------------------------------------------------------------


def test_a_hostname_on_an_edge_passes(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "edge1.example.net": {"203.0.113.5"},
        }
    ).check(site, deployed=True)
    assert _check(report, "dns").passed
    assert report.ok


def test_public_edge_addresses_are_distinct_from_the_ssh_host(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "localhost": {"127.0.0.1"},
            "203.0.113.5": {"203.0.113.5"},
        },
        edges=[
            {
                "host": "localhost",
                "public_addresses": ["203.0.113.5"],
            }
        ],
    ).check(site, deployed=True)
    assert _check(report, "dns").passed
    assert report.ok


def test_a_hostname_pointed_somewhere_else_blocks_and_names_both_sides(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"198.51.100.99"},
            "edge1.example.net": {"203.0.113.5"},
        }
    ).check(site, deployed=True)
    failure = _check(report, "dns")
    assert not failure.passed
    assert failure.severity is PreflightSeverity.BLOCKING
    # The operator has to be able to see what to change without a second tool.
    assert "198.51.100.99" in failure.detail
    assert "203.0.113.5" in failure.detail
    assert not report.ok


def test_an_unresolvable_hostname_blocks(build, site):
    report = build(addresses={"edge1.example.net": {"203.0.113.5"}}).check(
        site, deployed=True
    )
    assert "does not resolve" in _check(report, "dns").detail
    assert not report.ok


def test_an_inventory_with_no_usable_edge_blocks_rather_than_passing(build, site):
    """With nothing to compare against, the check cannot pass vacuously."""
    report = build(addresses={"cdn.example.com": {"203.0.113.5"}}, edges=[]).check(
        site, deployed=True
    )
    assert not _check(report, "dns").passed
    assert "no edge address" in _check(report, "dns").detail


def test_an_unreadable_inventory_does_not_raise(settings, monkeypatch, site):
    class ExplodingInventory:
        def list_edges(self):
            raise OSError("inventory is unreadable")

    monkeypatch.setattr(
        preflight_module, "_resolve", lambda host, resolver=None: {"203.0.113.5"}
    )
    monkeypatch.setattr(
        CertificatePreflight,
        "_closest_caa",
        lambda self, name: None,
    )
    report = CertificatePreflight(
        settings,
        inventory=ExplodingInventory(),  # type: ignore[arg-type]
        origin_probe=FakeOriginProbe(),  # type: ignore[arg-type]
    ).check(site, deployed=True)
    assert not _check(report, "dns").passed


def test_a_wildcard_site_is_not_resolved(build):
    wildcard = CdnSite(
        name="wildcard-example-com",
        server_names=("*.example.com",),
        origin_host="198.51.100.10",
    )
    report = build().check(wildcard, deployed=True)
    assert _check(report, "dns").passed
    assert "wildcard" in _check(report, "dns").detail


# ----------------------------------------------------------------------
# CAA
# ----------------------------------------------------------------------


def test_no_caa_record_anywhere_passes(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "edge1.example.net": {"203.0.113.5"},
        },
        caa=None,
    ).check(site, deployed=True)
    assert _check(report, "caa").passed


def test_caa_naming_another_ca_blocks_and_says_which(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "edge1.example.net": {"203.0.113.5"},
        },
        caa=("example.com", [("issue", "other-ca.example")]),
    ).check(site, deployed=True)
    failure = _check(report, "caa")
    assert not failure.passed
    assert failure.severity is PreflightSeverity.BLOCKING
    assert "other-ca.example" in failure.detail
    assert "letsencrypt.org" in failure.detail


def test_caa_permitting_our_ca_passes(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "edge1.example.net": {"203.0.113.5"},
        },
        caa=("example.com", [("issue", "letsencrypt.org")]),
    ).check(site, deployed=True)
    assert _check(report, "caa").passed
    assert report.ok


def test_a_broken_resolver_is_an_advisory_not_a_block(build, site):
    """A flaky resolver is not evidence of a hostile CAA record."""
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "edge1.example.net": {"203.0.113.5"},
        },
        caa_error=dns.exception.Timeout(),
    ).check(site, deployed=True)
    failure = _check(report, "caa")
    assert not failure.passed
    assert failure.severity is PreflightSeverity.ADVISORY
    assert report.ok


def test_a_configured_ca_is_what_caa_is_checked_against(build, site, settings):
    """Pointing certbot at another ACME server must move the CAA check with it."""
    moved = settings.model_copy(update={"acme_ca_domain": "other-ca.example"})
    checker = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "edge1.example.net": {"203.0.113.5"},
        },
        caa=("example.com", [("issue", "other-ca.example")]),
    )
    checker._settings = moved
    assert _check(checker.check(site, deployed=True), "caa").passed


# ----------------------------------------------------------------------
# Deployment, origin, TTL
# ----------------------------------------------------------------------


def test_an_undeployed_site_blocks_with_the_fix_named(build, site):
    report = build().check(site, deployed=False)
    failure = _check(report, "deployed")
    assert not failure.passed
    assert failure.severity is PreflightSeverity.BLOCKING
    assert "blitzecdn deploy" in failure.detail


def test_a_dead_origin_is_advisory_because_a_cert_is_still_issuable(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "edge1.example.net": {"203.0.113.5"},
        },
        origin_ok=False,
    ).check(site, deployed=True)
    failure = _check(report, "origin")
    assert not failure.passed
    assert failure.severity is PreflightSeverity.ADVISORY
    assert report.ok


def test_a_high_ttl_is_advisory_and_a_normal_one_passes(build, site):
    high = build().check(site, deployed=True, record_ttl=86400)
    assert not _check(high, "ttl").passed
    assert _check(high, "ttl").severity is PreflightSeverity.ADVISORY
    assert _check(build().check(site, deployed=True, record_ttl=300), "ttl").passed


def test_ttl_is_omitted_when_the_caller_does_not_supply_one(build, site):
    report = build().check(site, deployed=True)
    assert not [check for check in report.checks if check.name == "ttl"]


def test_summary_lists_only_blocking_failures(build, site):
    report = build(
        addresses={"edge1.example.net": {"203.0.113.5"}},
        origin_ok=False,
    ).check(site, deployed=False, record_ttl=86400)
    summary = report.summary()
    assert "dns" in summary
    assert "deployed" in summary
    assert "origin" not in summary
    assert "ttl" not in summary
    assert {check.name for check in report.advisories} == {"origin", "ttl"}


# ----------------------------------------------------------------------
# The two seams that reach the network
# ----------------------------------------------------------------------


def test_resolve_returns_a_literal_address_without_asking_the_resolver(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a literal address must not be sent to the resolver")

    monkeypatch.setattr(preflight_module.socket, "getaddrinfo", explode)
    assert preflight_module._resolve("203.0.113.5") == {"203.0.113.5"}
    assert preflight_module._resolve("  203.0.113.5.  ") == {"203.0.113.5"}


def test_resolve_normalizes_so_two_spellings_of_one_address_compare_equal(monkeypatch):
    monkeypatch.setattr(
        preflight_module.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, "", ("2001:0DB8:0000::1", 0, 0, 0))],
    )
    assert preflight_module._resolve("edge.example.net") == {"2001:db8::1"}


def test_resolve_treats_a_lookup_failure_as_no_addresses(monkeypatch):
    monkeypatch.setattr(
        preflight_module.socket,
        "getaddrinfo",
        lambda *a, **k: (_ for _ in ()).throw(OSError("NXDOMAIN")),
    )
    assert preflight_module._resolve("absent.example") == set()
    assert preflight_module._resolve("") == set()


class _FakeCaaRecord:
    def __init__(self, tag: bytes, value: bytes) -> None:
        self.tag = tag
        self.value = value


def test_closest_caa_stops_at_the_first_ancestor_that_answers(settings, monkeypatch):
    """RFC 8659: the nearest RRset decides; the parent's is not merged in."""
    asked: list[str] = []

    class FakeResolver:
        lifetime = 0.0
        timeout = 0.0

        def resolve(self, name, _rdtype):
            asked.append(name)
            if name == "cdn.example.com":
                raise dns.resolver.NoAnswer
            if name == "example.com":
                return [_FakeCaaRecord(b"ISSUE", b"letsencrypt.org")]
            raise AssertionError("the walk should have stopped at example.com")

    monkeypatch.setattr(dns.resolver, "Resolver", FakeResolver)
    found = CertificatePreflight(settings)._closest_caa("cdn.example.com")

    assert found == ("example.com", [("issue", "letsencrypt.org")])
    assert asked == ["cdn.example.com", "example.com"]


def test_closest_caa_returns_none_when_no_ancestor_has_a_record(settings, monkeypatch):
    class FakeResolver:
        lifetime = 0.0
        timeout = 0.0

        def resolve(self, name, _rdtype):
            raise dns.resolver.NXDOMAIN

    monkeypatch.setattr(dns.resolver, "Resolver", FakeResolver)
    assert CertificatePreflight(settings)._closest_caa("cdn.example.com") is None


# --- explicit preflight resolvers -------------------------------------------


class _Address:
    def __init__(self, address: str) -> None:
        self.address = address


class RecordingResolver:
    """Captures what it was asked and answers from a fixed table."""

    instances: ClassVar[list[RecordingResolver]] = []

    def __init__(self) -> None:
        self.nameservers: list[str] = []
        self.lifetime = 0.0
        self.timeout = 0.0
        self.asked: list[tuple[str, object]] = []
        RecordingResolver.instances.append(self)

    def resolve(self, name, rdatatype):
        self.asked.append((name, rdatatype))
        if rdatatype is dns.rdatatype.A and name == "www.example.com":
            return [_Address("203.0.113.9")]
        raise dns.resolver.NoAnswer


@pytest.fixture
def recording_resolver(monkeypatch):
    RecordingResolver.instances = []
    monkeypatch.setattr(dns.resolver, "Resolver", RecordingResolver)
    return RecordingResolver


def test_configured_servers_are_used_instead_of_the_host_resolver(
    settings, monkeypatch, recording_resolver
):
    """The controller's own view of DNS must not decide what the CA will see."""

    def explode(*args, **kwargs):
        raise AssertionError("getaddrinfo must not be used when servers are set")

    monkeypatch.setattr(preflight_module.socket, "getaddrinfo", explode)
    configured = settings.model_copy(
        update={"preflight_dns_servers": ("1.1.1.1", "1.0.0.1")}
    )
    addresses = preflight_module._resolve(
        "www.example.com",
        CertificatePreflight(configured, inventory=object())._address_resolver(),
    )
    assert addresses == {"203.0.113.9"}
    assert recording_resolver.instances[-1].nameservers == ["1.1.1.1", "1.0.0.1"]


def test_without_configured_servers_the_host_resolver_is_still_used(settings):
    """Default behaviour is unchanged, so /etc/hosts keeps applying."""
    assert (
        CertificatePreflight(settings, inventory=object())._address_resolver() is None
    )


def test_the_dns_check_names_the_resolver_it_asked(settings, monkeypatch, build):
    configured = settings.model_copy(update={"preflight_dns_servers": ("9.9.9.9",)})
    monkeypatch.setattr(
        preflight_module,
        "_resolve",
        lambda host, resolver=None: {"198.51.100.1"},
    )
    monkeypatch.setattr(CertificatePreflight, "_closest_caa", lambda self, name: None)

    class FakeInventory:
        def list_edges(self):
            return [{"host": "edge1.example.net"}]

    report = CertificatePreflight(
        configured, inventory=FakeInventory(), origin_probe=FakeOriginProbe()
    ).check(
        CdnSite(
            name="site",
            server_names=("www.example.com",),
            origin_host="origin.example.com",
        ),
        deployed=True,
    )
    detail = next(check.detail for check in report.checks if check.name == "dns")
    assert "via 9.9.9.9" in detail


# --- resolver honesty probe --------------------------------------------------


def test_a_resolver_that_rejects_reserved_names_passes(settings, recording_resolver):
    check = preflight_module.check_resolver(settings)
    assert check.passed
    probed = recording_resolver.instances[-1].asked[0][0]
    assert probed.endswith(".invalid"), "probe must use a name that cannot be delegated"


def test_a_resolver_that_answers_a_reserved_name_blocks(settings, monkeypatch):
    """The exact pathology of a transparent proxy claiming every hostname."""

    class LyingResolver:
        def __init__(self) -> None:
            self.nameservers: list[str] = []
            self.lifetime = 0.0
            self.timeout = 0.0

        def resolve(self, name, rdatatype):
            return [_Address("172.16.200.20")]

    monkeypatch.setattr(dns.resolver, "Resolver", LyingResolver)
    check = preflight_module.check_resolver(settings)
    assert not check.passed
    assert check.severity is PreflightSeverity.BLOCKING
    assert "172.16.200.20" in check.detail
    assert "invents addresses" in check.detail


def test_an_unreachable_resolver_is_not_reported_as_dishonest(settings, monkeypatch):
    class DeadResolver:
        def __init__(self) -> None:
            self.nameservers: list[str] = []
            self.lifetime = 0.0
            self.timeout = 0.0

        def resolve(self, name, rdatatype):
            raise dns.exception.Timeout

    monkeypatch.setattr(dns.resolver, "Resolver", DeadResolver)
    assert preflight_module.check_resolver(settings).passed
