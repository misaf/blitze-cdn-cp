import re
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from blitzecdn.domain.cache import (
    CacheStatsReport,
    EdgeStats,
    PurgeEntry,
    PurgeResult,
    SiteCacheStats,
)
from blitzecdn.domain.certificates import (
    CertificateInfo,
    CertificateSource,
    CertificateStatus,
)
from blitzecdn.domain.deployments import (
    DEPLOYMENT_TRANSITIONS,
    TERMINAL_STATUSES,
    DeploymentStatus,
    is_terminal,
    require_transition,
)
from blitzecdn.domain.dns import DnsRecord, RecordPatch
from blitzecdn.domain.runs import HostRun
from blitzecdn.domain.sites import CdnSite, SitePolicy, SslMode
from blitzecdn.domain.snapshots import decode_snapshot


def test_site_normalizes_safe_hostnames(site_payload):
    site_payload["server_names"] = ["CDN.Example.COM.", "*.assets.example.com"]
    site_payload["origin_host"] = "Origin.Example.COM."
    site = CdnSite.model_validate(site_payload)
    assert site.server_names == ("cdn.example.com", "*.assets.example.com")
    assert site.origin_host == "origin.example.com"


@pytest.mark.parametrize(
    "snapshot",
    [
        "{}",
        '{"domains": [], "records": [], "unknown": []}',
        '{"domains": {}, "records": []}',
    ],
)
def test_snapshots_fail_closed_on_incomplete_or_unknown_shapes(snapshot):
    with pytest.raises(ValueError, match="deployment snapshot"):
        decode_snapshot(snapshot)


@pytest.mark.parametrize(
    "value", ["*.192.0.2.1", "*.203.0.113.0", "*.::1", "*.2001:db8::1"]
)
def test_site_rejects_a_wildcard_on_an_ip_address(site_payload, value):
    """nginx accepts `server_name *.192.0.2.1` and then matches nothing.

    Every label of an IPv4 literal is a valid DNS label, so the fallback that
    accepts ordinary hostnames used to accept this one too — the refusal was
    raised inside a `try` whose own `except ValueError` swallowed it. The result
    rendered, converged, and silently matched no request ever sent.

    All four now fail on the wildcard guard itself. The v6 pair used to be
    turned away one step later, as malformed hostnames, because ':' fails the
    label check — right answer, wrong reason, and it said nothing about the
    wildcard being the actual problem.
    """
    site_payload["server_names"] = [value]
    with pytest.raises(
        ValidationError, match=re.escape("wildcards cannot be used with IP addresses")
    ):
        CdnSite.model_validate(site_payload)


def test_a_bare_ip_is_still_a_usable_server_name(site_payload):
    """Only the wildcard form is nonsense; the address itself is addressable."""
    site_payload["server_names"] = ["192.0.2.1"]
    assert CdnSite.model_validate(site_payload).server_names == ("192.0.2.1",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "../site"),
        ("server_names", ["example.com; return 200"]),
        ("origin_host", "origin.example.com/path"),
        ("cache_valid_success", "10 minutes"),
        ("certificate_path", "../../secret"),
    ],
)
def test_site_rejects_injection_and_path_traversal(site_payload, field, value):
    site_payload[field] = value
    with pytest.raises(ValidationError):
        CdnSite.model_validate(site_payload)


def test_existing_certificate_requires_complete_pair(site_payload):
    site_payload["certificate_mode"] = "existing"
    site_payload["certificate_path"] = "/etc/ssl/example/fullchain.pem"
    with pytest.raises(ValidationError, match="both certificate paths"):
        CdnSite.model_validate(site_payload)


def test_new_sites_default_to_ssl_off(site_payload):
    site = CdnSite.model_validate(site_payload)
    assert site.ssl_mode is SslMode.OFF
    assert site.serves_tls is False
    assert site.ssl_mode.origin_scheme == "http"


@pytest.mark.parametrize("mode", ["flexible", "full", "full_strict"])
def test_secure_ssl_modes_require_an_edge_certificate(site_payload, mode):
    site_payload["ssl_mode"] = mode
    with pytest.raises(ValidationError, match="active edge certificate"):
        CdnSite.model_validate(site_payload)


def test_off_keeps_an_installed_certificate_available(site_payload):
    site_payload |= {
        "ssl_mode": "off",
        "certificate_mode": "existing",
        "certificate_path": "/etc/ssl/certs/edge.pem",
        "certificate_key_path": "/etc/ssl/private/edge.key",
    }
    site = CdnSite.model_validate(site_payload)
    assert site.certificate_mode == "existing"
    assert site.serves_tls is False


@pytest.mark.parametrize(
    ("scheme", "certificate_mode", "expected"),
    [
        ("http", "disabled", SslMode.OFF),
        ("https", "disabled", SslMode.OFF),
        ("http", "existing", SslMode.FLEXIBLE),
        ("https", "existing", SslMode.FULL_STRICT),
    ],
)
def test_legacy_origin_scheme_maps_to_ssl_mode(
    site_payload, scheme, certificate_mode, expected
):
    site_payload["origin_scheme"] = scheme
    if certificate_mode == "existing":
        site_payload |= {
            "certificate_mode": certificate_mode,
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
    site = CdnSite.model_validate(site_payload)
    assert site.ssl_mode is expected
    assert "origin_scheme" not in site.model_dump()


def test_new_and_legacy_ssl_fields_cannot_be_combined(site_payload):
    site_payload |= {"ssl_mode": "off", "origin_scheme": "http"}
    with pytest.raises(ValidationError, match="cannot be combined"):
        CdnSite.model_validate(site_payload)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/cron.d/blitzecdn",
        "/root/.ssh/authorized_keys",
        "/etc/nginx/conf.d/evil.conf",
        "/var/lib/blitzecdn/tls/fullchain.pem",
    ],
)
def test_certificate_paths_stay_inside_certificate_directories(site_payload, path):
    """Deploys copy these paths as root, so they must not escape TLS directories."""
    site_payload["certificate_mode"] = "existing"
    site_payload["certificate_path"] = path
    site_payload["certificate_key_path"] = "/etc/ssl/example/privkey.pem"
    with pytest.raises(ValidationError, match="must live under"):
        CdnSite.model_validate(site_payload)


def test_managed_certificate_modes_reject_operator_chosen_paths(site_payload):
    site_payload["certificate_mode"] = "uploaded"
    site_payload["certificate_path"] = "/etc/ssl/elsewhere/fullchain.pem"
    site_payload["certificate_key_path"] = "/etc/ssl/elsewhere/privkey.pem"
    with pytest.raises(ValidationError, match="upload and request endpoints"):
        CdnSite.model_validate(site_payload)


def test_managed_certificate_modes_accept_their_own_paths(site_payload):
    site_payload["certificate_mode"] = "uploaded"
    site_payload["certificate_path"] = (
        "/etc/blitzecdn/tls/cdn-example-com/fullchain.pem"
    )
    site_payload["certificate_key_path"] = (
        "/etc/blitzecdn/tls/cdn-example-com/privkey.pem"
    )
    assert CdnSite.model_validate(site_payload).certificate_mode == "uploaded"


def _managed_record(**overrides: object) -> dict[str, object]:
    return {
        "domain": "example.com",
        "name": "cdn",
        "value": "198.51.100.10",
        "proxied": True,
        "certificate_mode": "uploaded",
        "certificate_path": "/etc/blitzecdn/tls/cdn-example-com/fullchain.pem",
        "certificate_key_path": "/etc/blitzecdn/tls/cdn-example-com/privkey.pem",
        **overrides,
    }


def test_record_patch_cannot_redirect_a_managed_certificate():
    """The escalation path: aim a managed record's cert at an arbitrary file.

    A deploy writes these paths as root, so the record has to be refused before
    it reaches the derived desired state.
    """
    record = DnsRecord.model_validate(_managed_record())
    patch = RecordPatch(certificate_path="/etc/cron.d/blitzecdn")
    with pytest.raises(ValidationError):
        DnsRecord.model_validate(
            {**record.model_dump(), **patch.model_dump(exclude_unset=True)}
        )


def test_record_patch_revalidates_the_whole_record():
    record = DnsRecord.model_validate(_managed_record())
    patch = RecordPatch(value="192.0.2.20", cache_enabled=False)
    updated = DnsRecord.model_validate(
        {**record.model_dump(), **patch.model_dump(exclude_unset=True)}
    )
    assert updated.value == "192.0.2.20"
    assert updated.cache_enabled is False
    assert updated.to_site().origin_host == "192.0.2.20"


def test_record_patch_covers_every_shared_policy_field():
    """`RecordPatch` cannot inherit `SitePolicy`, so nothing else keeps it honest.

    Every policy field has to be patchable. One missing is not an error anyone
    sees: the API accepts the request, silently drops the unknown key under
    `extra="forbid"`... or worse, rejects a field an operator can set on
    creation but never change. Adding a field to `SitePolicy` should fail here
    until it is added below it too.
    """
    assert set(SitePolicy.model_fields) <= set(RecordPatch.model_fields), (
        "RecordPatch is missing "
        f"{sorted(set(SitePolicy.model_fields) - set(RecordPatch.model_fields))}. "
        "Add the field to RecordPatch as an optional defaulting to None."
    )


def test_every_patchable_policy_field_is_optional():
    """An inherited required field would arrive here with a default and apply
    itself on every unrelated patch."""
    for name in SitePolicy.model_fields:
        assert RecordPatch.model_fields[name].default is None, (
            f"RecordPatch.{name} must default to None so an unset field means "
            "'leave alone' rather than 'reset to this value'"
        )


def test_a_site_derived_from_a_record_carries_every_policy_field():
    """`to_site()` copies the policy by name; prove nothing is lost in transit."""
    record = DnsRecord.model_validate(
        _managed_record(
            origin_port=8443, cache_valid_success="30m", origin_sni="o.test"
        )
    )
    site = record.to_site()
    for name in SitePolicy.model_fields:
        assert getattr(site, name) == getattr(record, name), (
            f"{name} did not survive DnsRecord.to_site()"
        )


def _info(days: int, source: CertificateSource) -> CertificateInfo:
    now = datetime.now(UTC)
    return CertificateInfo(
        site="cdn-example-com",
        source=source,
        domains=("cdn.example.com",),
        not_before=now - timedelta(days=1),
        not_after=now + timedelta(days=days),
        fingerprint_sha256="ab" * 32,
    )


@pytest.mark.parametrize(
    ("days", "expected_remaining", "expired"),
    [(60, 60, False), (1, 1, False), (0, 0, False), (-1, -1, True)],
)
def test_certificate_status_counts_whole_days_and_notices_expiry(
    days, expected_remaining, expired
):
    now = datetime.now(UTC)
    status = CertificateStatus.of(_info(days, CertificateSource.ACME), now=now)
    assert status.days_remaining == expected_remaining
    assert status.expired is expired


def test_a_certificate_with_hours_left_does_not_round_up_to_a_reassuring_day():
    """Truncating toward zero here would report 1 day for something due tonight."""
    now = datetime.now(UTC)
    info = _info(1, CertificateSource.ACME).model_copy(
        update={"not_after": now + timedelta(hours=6)}
    )
    assert CertificateStatus.of(info, now=now).days_remaining == 0


def test_only_acme_certificates_are_renewable():
    now = datetime.now(UTC)
    acme = CertificateStatus.of(_info(10, CertificateSource.ACME), now=now)
    uploaded = CertificateStatus.of(_info(10, CertificateSource.UPLOADED), now=now)

    assert acme.renewable is True
    assert acme.due_for_renewal() is True
    assert uploaded.renewable is False
    assert uploaded.due_for_renewal() is False, (
        "BlitzeCDN cannot reissue a certificate someone else supplied"
    )


def test_a_certificate_outside_the_window_is_not_due():
    now = datetime.now(UTC)
    status = CertificateStatus.of(_info(60, CertificateSource.ACME), now=now)
    assert status.due_for_renewal() is False
    assert status.due_for_renewal(within_days=90) is True


# ----------------------------------------------------------------------
# Cache purge and statistics
# ----------------------------------------------------------------------


def test_a_purge_uri_must_be_an_absolute_path():
    for bad in ("app.js", "", "  ", "//host/x".replace("//host", "http://host")):
        with pytest.raises(ValidationError):
            PurgeEntry(host="cdn.example.com", uri=bad)


def test_a_purge_uri_keeps_the_path_exactly_as_the_cache_keyed_it():
    """$request_uri is the key, so normalizing here would purge the wrong key."""
    entry = PurgeEntry(host="cdn.example.com", uri="  /a/./b?v=2  ")
    assert entry.uri == "/a/./b?v=2"


def test_a_purge_uri_cannot_contain_whitespace():
    with pytest.raises(ValidationError):
        PurgeEntry(host="cdn.example.com", uri="/a b")


def test_a_purge_host_is_normalized_like_every_other_hostname():
    assert PurgeEntry(host="CDN.Example.COM.", uri="/").host == "cdn.example.com"


def test_a_purge_is_incomplete_when_any_edge_failed():
    """A partial purge serves different bytes depending on which edge answers."""
    result = PurgeResult(
        purged_at=datetime.now(UTC),
        hosts=(
            HostRun(host="edge-a", changed=1),
            HostRun(host="edge-b", unreachable=1),
        ),
    )
    assert result.complete is False
    assert [host.host for host in result.succeeded] == ["edge-a"]
    assert [host.host for host in result.failed] == ["edge-b"]


def test_a_purge_that_reached_no_edge_is_not_complete():
    assert PurgeResult(purged_at=datetime.now(UTC)).complete is False


def test_revalidated_counts_as_a_hit_and_expired_does_not():
    """REVALIDATED served the stored body; EXPIRED re-fetched it."""
    stats = SiteCacheStats(
        site="cdn.example.com",
        outcomes={"HIT": 6, "REVALIDATED": 2, "EXPIRED": 1, "MISS": 1},
    )
    assert stats.hits == 8
    assert stats.cacheable_requests == 10
    assert stats.hit_ratio == 0.8


def test_requests_that_never_consulted_the_cache_are_left_out_of_the_ratio():
    """A redirect-heavy site would otherwise look like a broken cache."""
    stats = SiteCacheStats(site="cdn.example.com", outcomes={"HIT": 1, "NONE": 99})
    assert stats.requests == 100
    assert stats.cacheable_requests == 1
    assert stats.hit_ratio == 1.0


def test_a_site_with_no_cacheable_traffic_has_no_hit_ratio():
    """None, not zero: an idle site must not read as a failing one."""
    assert SiteCacheStats(site="a", outcomes={"NONE": 5}).hit_ratio is None
    assert SiteCacheStats(site="a").hit_ratio is None


def test_the_fleet_hit_ratio_is_weighted_by_requests_not_by_edge():
    """Averaging per-edge ratios would let a quiet edge outvote a busy one."""
    report = CacheStatsReport(
        collected_at=datetime.now(UTC),
        edges=(
            EdgeStats(
                host="busy",
                sites=(SiteCacheStats(site="a", outcomes={"HIT": 999, "MISS": 1}),),
            ),
            EdgeStats(
                host="quiet",
                sites=(SiteCacheStats(site="a", outcomes={"MISS": 1}),),
            ),
        ),
    )
    # Mean of the two edge ratios would be ~0.50. Weighted, it is 999/1001.
    assert report.hit_ratio == 0.998


def test_a_silent_edge_is_excluded_from_the_numbers_but_still_reported():
    report = CacheStatsReport(
        collected_at=datetime.now(UTC),
        edges=(
            EdgeStats(
                host="ok", sites=(SiteCacheStats(site="a", outcomes={"HIT": 1}),)
            ),
            EdgeStats(host="down", error="unreachable"),
        ),
    )
    assert [edge.host for edge in report.reporting] == ["ok"]
    assert [edge.host for edge in report.silent] == ["down"]
    assert report.hit_ratio == 1.0


def test_by_site_sums_a_site_across_every_edge_serving_it():
    report = CacheStatsReport(
        collected_at=datetime.now(UTC),
        edges=(
            EdgeStats(
                host="edge-a",
                sites=(
                    SiteCacheStats(
                        site="a.example.com", outcomes={"HIT": 3, "MISS": 1}
                    ),
                    SiteCacheStats(site="b.example.com", outcomes={"HIT": 1}),
                ),
            ),
            EdgeStats(
                host="edge-b",
                sites=(
                    SiteCacheStats(
                        site="a.example.com", outcomes={"HIT": 1, "MISS": 3}
                    ),
                ),
            ),
        ),
    )
    merged = {site.site: site for site in report.by_site()}
    assert merged["a.example.com"].outcomes == {"HIT": 4, "MISS": 4}
    assert merged["a.example.com"].hit_ratio == 0.5
    assert merged["b.example.com"].hit_ratio == 1.0


# ----------------------------------------------------------------------
# Deployment lifecycle
# ----------------------------------------------------------------------


def test_every_deployment_status_is_in_a_transition_row_or_terminal():
    """Nothing the enum can name is unreachable or dead weight."""
    for status in DeploymentStatus:
        assert is_terminal(status) or status in DEPLOYMENT_TRANSITIONS
        if is_terminal(status):
            assert status not in DEPLOYMENT_TRANSITIONS, (
                f"terminal status {status.value} must not list further transitions"
            )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DeploymentStatus.QUEUED, DeploymentStatus.RUNNING),
        (DeploymentStatus.QUEUED, DeploymentStatus.FAILED),
        (DeploymentStatus.RUNNING, DeploymentStatus.SUCCEEDED),
        (DeploymentStatus.RUNNING, DeploymentStatus.FAILED),
        (DeploymentStatus.RUNNING, DeploymentStatus.TIMED_OUT),
        (DeploymentStatus.RUNNING, DeploymentStatus.ABANDONED),
    ],
)
def test_the_lifecycle_allows_these_transitions(current, target):
    require_transition(current, target)  # must not raise


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DeploymentStatus.QUEUED, DeploymentStatus.SUCCEEDED),
        (DeploymentStatus.QUEUED, DeploymentStatus.TIMED_OUT),
        (DeploymentStatus.QUEUED, DeploymentStatus.ABANDONED),
        (DeploymentStatus.RUNNING, DeploymentStatus.QUEUED),
        (DeploymentStatus.SUCCEEDED, DeploymentStatus.RUNNING),
        (DeploymentStatus.SUCCEEDED, DeploymentStatus.SUCCEEDED),
        (DeploymentStatus.FAILED, DeploymentStatus.SUCCEEDED),
        (DeploymentStatus.TIMED_OUT, DeploymentStatus.ABANDONED),
        (DeploymentStatus.ABANDONED, DeploymentStatus.RUNNING),
    ],
)
def test_the_lifecycle_refuses_these_transitions(current, target):
    with pytest.raises(ValueError, match="illegal deployment transition"):
        require_transition(current, target)


def test_terminal_statuses_are_closed():
    for status in TERMINAL_STATUSES:
        assert is_terminal(status)
        with pytest.raises(ValueError):
            require_transition(status, DeploymentStatus.RUNNING)
