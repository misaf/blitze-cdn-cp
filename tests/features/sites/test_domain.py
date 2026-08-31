import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from blitzecdn.core.runs import HostRun
from blitzecdn.features.cache.domain import (
    CacheStatsReport,
    EdgeStats,
    PurgeEntry,
    PurgeResult,
    SiteCacheStats,
)
from blitzecdn.features.deployments.domain import (
    DEPLOYMENT_TRANSITIONS,
    TERMINAL_STATUSES,
    DeploymentStatus,
    is_terminal,
    require_transition,
)
from blitzecdn.features.deployments.snapshots import decode_snapshot, encode_snapshot
from blitzecdn.features.dns.domain import DnsRecord, RecordPatch
from blitzecdn.features.http.policy import (
    DEFAULT_PORTS,
    HTTP_PROXY_PORTS,
    HTTPS_PROXY_PORTS,
    HttpScheme,
)
from blitzecdn.features.security.policy import SiteFirewall
from blitzecdn.features.sites.domain import CdnSite, SitePolicy
from blitzecdn.features.sites.policy import CacheQueryStringMode, SiteVisitorHeaders
from blitzecdn.features.tls.certificates.domain import (
    CertificateInfo,
    CertificateSource,
    CertificateStatus,
)
from blitzecdn.features.tls.policy import MinimumTlsVersion, SslAutomaticMode, SslMode


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


def test_snapshots_fail_closed_on_unknown_schema_versions():
    snapshot = '{"schema_version":999,"domains":[],"records":[]}'
    with pytest.raises(ValueError, match="unsupported deployment snapshot"):
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
    assert site.ssl_automatic_mode is SslAutomaticMode.AUTO
    assert site.minimum_tls_version is MinimumTlsVersion.TLS_1_2
    assert site.http3_enabled is False
    assert site.cache_query_string_mode is CacheQueryStringMode.INCLUDE
    assert site.serves_tls is False
    assert site.canonical_origin_scheme is HttpScheme.HTTP


def test_http3_requires_edge_tls(site_payload):
    site_payload["http3_enabled"] = True
    with pytest.raises(ValidationError, match="http3_enabled=True requires ssl_mode"):
        CdnSite.model_validate(site_payload)


def test_http3_accepts_tls_with_a_tls_1_2_tcp_minimum(site_payload):
    site_payload |= {
        "ssl_mode": "flexible",
        "http3_enabled": True,
        "minimum_tls_version": "1.2",
        "certificate_mode": "existing",
        "certificate_path": "/etc/ssl/certs/edge.pem",
        "certificate_key_path": "/etc/ssl/private/edge.key",
    }
    site = CdnSite.model_validate(site_payload)
    assert site.http3_enabled is True
    assert site.minimum_tls_version is MinimumTlsVersion.TLS_1_2


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


#: The whole SSL-mode x visitor-protocol x listener-port matrix, in one table.
#: Off has no HTTPS listener, so its HTTPS rows cannot arise in practice; the
#: method still answers for them, and the answer is HTTP, because Off never
#: encrypts anything.
#:
#: Flexible is the only mode whose answer varies with the port, and it is the
#: reason the port is a parameter at all: Cloudflare supports Flexible for HTTPS
#: on 443 only, and falls back to Full-like transport on the five alternate
#: HTTPS proxy ports.
_ORIGIN_SCHEMES = [
    (SslMode.OFF, HttpScheme.HTTP, 80, HttpScheme.HTTP),
    (SslMode.OFF, HttpScheme.HTTP, 8080, HttpScheme.HTTP),
    (SslMode.OFF, HttpScheme.HTTPS, 443, HttpScheme.HTTP),
    (SslMode.OFF, HttpScheme.HTTPS, 8443, HttpScheme.HTTP),
    (SslMode.FLEXIBLE, HttpScheme.HTTP, 80, HttpScheme.HTTP),
    (SslMode.FLEXIBLE, HttpScheme.HTTP, 8080, HttpScheme.HTTP),
    (SslMode.FLEXIBLE, HttpScheme.HTTP, 2052, HttpScheme.HTTP),
    (SslMode.FLEXIBLE, HttpScheme.HTTPS, 443, HttpScheme.HTTP),
    (SslMode.FLEXIBLE, HttpScheme.HTTPS, 2053, HttpScheme.HTTPS),
    (SslMode.FLEXIBLE, HttpScheme.HTTPS, 2083, HttpScheme.HTTPS),
    (SslMode.FLEXIBLE, HttpScheme.HTTPS, 2087, HttpScheme.HTTPS),
    (SslMode.FLEXIBLE, HttpScheme.HTTPS, 2096, HttpScheme.HTTPS),
    (SslMode.FLEXIBLE, HttpScheme.HTTPS, 8443, HttpScheme.HTTPS),
    (SslMode.FULL, HttpScheme.HTTP, 80, HttpScheme.HTTP),
    (SslMode.FULL, HttpScheme.HTTP, 8080, HttpScheme.HTTP),
    (SslMode.FULL, HttpScheme.HTTPS, 443, HttpScheme.HTTPS),
    (SslMode.FULL, HttpScheme.HTTPS, 8443, HttpScheme.HTTPS),
    (SslMode.FULL_STRICT, HttpScheme.HTTP, 80, HttpScheme.HTTP),
    (SslMode.FULL_STRICT, HttpScheme.HTTP, 2052, HttpScheme.HTTP),
    (SslMode.FULL_STRICT, HttpScheme.HTTPS, 443, HttpScheme.HTTPS),
    (SslMode.FULL_STRICT, HttpScheme.HTTPS, 2053, HttpScheme.HTTPS),
]


@pytest.mark.parametrize(("mode", "visitor", "port", "origin"), _ORIGIN_SCHEMES)
def test_origin_scheme_follows_the_mode_the_visitor_and_the_port(
    mode, visitor, port, origin
):
    assert mode.origin_scheme_for(visitor, port) is origin


@pytest.mark.parametrize("port", HTTPS_PROXY_PORTS)
def test_flexible_is_flexible_on_443_and_full_like_everywhere_else(port):
    """The Cloudflare compatibility rule this parameter exists for.

    Treating Flexible as one global origin protocol sent an HTTPS visitor on
    8443 to a plaintext ``http://origin:8443``. Cloudflare supports Flexible on
    443 only; every other HTTPS proxy port falls back to Full.
    """
    expected = HttpScheme.HTTP if port == 443 else HttpScheme.HTTPS
    assert SslMode.FLEXIBLE.origin_scheme_for(HttpScheme.HTTPS, port) is expected


def test_the_flexible_fallback_is_full_and_never_full_strict():
    """Only the transport falls back. Verification is a separate question.

    An origin serving Flexible was never asked for a certificate the edge could
    validate, so turning on verification along with TLS would break every one of
    them the moment a visitor used an alternate port.
    """
    assert (
        SslMode.FLEXIBLE.origin_scheme_for(HttpScheme.HTTPS, 8443) is HttpScheme.HTTPS
    )
    assert SslMode.FLEXIBLE.verifies_origin is False


@pytest.mark.parametrize("mode", [SslMode.FULL, SslMode.FULL_STRICT])
@pytest.mark.parametrize("port", HTTP_PROXY_PORTS)
def test_full_modes_do_not_re_originate_http_as_https(mode, port):
    """The regression this method exists to prevent.

    A property keyed on the mode alone answered HTTPS for every request, so a
    visitor arriving on a plaintext listener was proxied to a TLS origin port
    that, for most origins, is not listening at all.
    """
    assert mode.origin_scheme_for(HttpScheme.HTTP, port) is HttpScheme.HTTP


@pytest.mark.parametrize("mode", list(SslMode))
@pytest.mark.parametrize("port", HTTP_PROXY_PORTS)
def test_no_mode_encrypts_the_origin_leg_of_a_plaintext_visitor(mode, port):
    assert mode.origin_scheme_for(HttpScheme.HTTP, port) is HttpScheme.HTTP


def test_the_proxy_port_sets_are_independent_and_disjoint():
    """8080 is not 8443's partner, and nothing in the domain pairs them."""
    assert set(HTTP_PROXY_PORTS) & set(HTTPS_PROXY_PORTS) == set()
    assert len(HTTP_PROXY_PORTS) + len(HTTPS_PROXY_PORTS) == 13
    assert DEFAULT_PORTS[HttpScheme.HTTP] in HTTP_PROXY_PORTS
    assert DEFAULT_PORTS[HttpScheme.HTTPS] in HTTPS_PROXY_PORTS


@pytest.mark.parametrize(
    ("mode", "verifies"),
    [
        (SslMode.OFF, False),
        (SslMode.FLEXIBLE, False),
        (SslMode.FULL, False),
        (SslMode.FULL_STRICT, True),
    ],
)
def test_only_full_strict_verifies_the_origin_certificate(mode, verifies):
    assert mode.verifies_origin is verifies


@pytest.mark.parametrize(
    ("mode", "visitor", "origin"),
    [
        (SslMode.OFF, HttpScheme.HTTP, HttpScheme.HTTP),
        (SslMode.FLEXIBLE, HttpScheme.HTTPS, HttpScheme.HTTP),
        (SslMode.FULL, HttpScheme.HTTPS, HttpScheme.HTTPS),
        (SslMode.FULL_STRICT, HttpScheme.HTTPS, HttpScheme.HTTPS),
    ],
)
def test_the_canonical_endpoint_is_the_one_preflight_probes(
    site_payload, mode, visitor, origin
):
    """Preflight checks one endpoint, not one per supported proxy port."""
    payload = site_payload | {"ssl_mode": mode}
    if mode is not SslMode.OFF:
        payload |= {
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
    site = CdnSite.model_validate(payload)
    assert site.canonical_visitor_scheme is visitor
    assert site.canonical_origin_scheme is origin


def _tls_payload(site_payload, mode, **extra):
    return (
        site_payload
        | {
            "ssl_mode": mode,
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
        | extra
    )


@pytest.mark.parametrize("mode", [SslMode.FLEXIBLE, SslMode.FULL, SslMode.FULL_STRICT])
def test_always_use_https_redirects_once_the_site_serves_tls(site_payload, mode):
    site = CdnSite.model_validate(
        _tls_payload(site_payload, mode, always_use_https=True)
    )
    assert site.redirects_http_to_https is True


def test_always_use_https_is_inert_under_ssl_off(site_payload):
    """Cloudflare hides the Always Use HTTPS control while the mode is Off.

    Off serves no HTTPS listener, so a redirect to HTTPS would send every
    visitor to a port the edge does not answer on — a dead end, and with a
    permanent 301 a cached one. The stored preference is kept rather than
    rejected or erased, exactly as Cloudflare keeps the zone setting: it takes
    effect the moment a secure mode is selected, in either order.
    """
    site = CdnSite.model_validate(site_payload | {"always_use_https": True})

    assert site.ssl_mode is SslMode.OFF
    assert site.always_use_https is True
    assert site.serves_tls is False
    assert site.redirects_http_to_https is False


def test_turning_tls_on_activates_a_preference_set_while_off(site_payload):
    off = CdnSite.model_validate(site_payload | {"always_use_https": True})
    on = CdnSite.model_validate(
        _tls_payload(site_payload, SslMode.FULL, always_use_https=True)
    )
    assert (off.redirects_http_to_https, on.redirects_http_to_https) == (False, True)


def test_a_tls_site_without_always_use_https_serves_both_schemes(site_payload):
    site = CdnSite.model_validate(_tls_payload(site_payload, SslMode.FULL))
    assert site.always_use_https is False
    assert site.redirects_http_to_https is False


def test_removed_origin_scheme_is_rejected(site_payload):
    site_payload["origin_scheme"] = "http"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CdnSite.model_validate(site_payload)
    assert "origin_scheme" not in RecordPatch.model_fields


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
        _managed_record(cache_valid_success="30m", origin_sni="o.test")
    )
    site = record.to_site()
    for name in SitePolicy.model_fields:
        assert getattr(site, name) == getattr(record, name), (
            f"{name} did not survive DnsRecord.to_site()"
        )


def test_under_attack_mode_defaults_off_and_is_patchable():
    assert SitePolicy().under_attack_mode is False
    assert RecordPatch(under_attack_mode=True).under_attack_mode is True

    record = DnsRecord.model_validate(_managed_record(under_attack_mode=True))
    site = record.to_site()
    assert site is not None
    assert site.under_attack_mode is True


def test_visitor_headers_default_to_the_address_and_not_the_country(site_payload):
    """The default has to be deployable on an edge with GeoIP off.

    `connecting_ip` costs nothing and answers a question the origin cannot
    answer for itself, so it is on. `ip_country` needs a database the edge role
    does not install by default, and defaulting it on would fail the next
    converge of every existing site.
    """
    site = CdnSite.model_validate(site_payload)

    assert site.visitor_headers == SiteVisitorHeaders()
    assert site.visitor_headers.connecting_ip is True
    assert site.visitor_headers.ip_country is False
    assert site.requires_geoip is False


def test_visitor_headers_reject_a_field_they_do_not_declare(site_payload):
    """No aliases, and nothing that looks like one — `extra="forbid"`."""
    for unknown in ("cf_connecting_ip", "true_client_ip", "connectingip"):
        with pytest.raises(ValidationError):
            CdnSite.model_validate(site_payload | {"visitor_headers": {unknown: True}})


def test_ip_country_requires_geoip_on_its_own(site_payload):
    """The header needs $blitzecdn_country whether or not a rule also does.

    Composed in one property rather than asked twice: the edge role's
    validation mirrors exactly this question, and a third consumer that added
    its own condition there would leave this one silently incomplete.
    """
    site = CdnSite.model_validate(
        site_payload | {"visitor_headers": {"ip_country": True}}
    )
    assert site.visitor_headers.requires_geoip is True
    assert site.firewall.requires_geoip is False
    assert site.requires_geoip is True


def test_country_firewall_rules_still_require_geoip_by_themselves(site_payload):
    """The original consumer must not have been displaced by the new one."""
    for rules in ({"denied_countries": ["RU"]}, {"allowed_countries": ["DE"]}):
        site = CdnSite.model_validate(site_payload | {"firewall": rules})
        assert site.firewall.requires_geoip is True
        assert site.visitor_headers.requires_geoip is False
        assert site.requires_geoip is True


def test_a_patch_replaces_the_whole_visitor_header_block():
    """Like the firewall, and for the same reason: partial merges cannot
    express turning the last switch off."""
    record = DnsRecord.model_validate(
        {
            "domain": "example.com",
            "name": "cdn",
            "value": "203.0.113.10",
            "proxied": True,
            "visitor_headers": {"connecting_ip": True, "ip_country": True},
        }
    )
    patch = RecordPatch(visitor_headers=SiteVisitorHeaders())
    updated = DnsRecord.model_validate(
        {**record.model_dump(), **patch.model_dump(exclude_unset=True)}
    )

    assert updated.visitor_headers.connecting_ip is True
    assert updated.visitor_headers.ip_country is False


def test_visitor_headers_survive_a_snapshot_round_trip():
    """Desired state is JSON on the way to a run and back from a rollback."""
    record = DnsRecord.model_validate(
        {
            "domain": "example.com",
            "name": "cdn",
            "value": "203.0.113.10",
            "proxied": True,
            "visitor_headers": {"connecting_ip": False, "ip_country": True},
        }
    )
    snapshot = encode_snapshot([], [record])

    (site,) = decode_snapshot(snapshot)

    assert site.visitor_headers.connecting_ip is False
    assert site.visitor_headers.ip_country is True
    assert site.requires_geoip is True


def test_http3_survives_a_snapshot_round_trip():
    record = DnsRecord.model_validate(
        _managed_record(ssl_mode="flexible", http3_enabled=True)
    )
    (site,) = decode_snapshot(encode_snapshot([], [record]))
    assert site.http3_enabled is True


def test_http3_changes_snapshot_identity_only_when_the_value_changes():
    disabled = DnsRecord.model_validate(
        _managed_record(ssl_mode="flexible", http3_enabled=False)
    )
    enabled = disabled.model_copy(update={"http3_enabled": True})

    baseline = encode_snapshot([], [disabled])
    assert encode_snapshot([], [disabled]) == baseline
    assert encode_snapshot([], [enabled]) != baseline


def test_a_snapshot_written_before_visitor_headers_still_decodes():
    """Successful deployments are rollback targets forever.

    Nothing in the repository persists a record without the block, so this is
    the only place the shape can be proven readable: the defaults apply, and a
    rollback to a pre-feature snapshot converges the pre-feature behaviour.
    """
    legacy = json.dumps(
        {
            "domains": [{"name": "example.com"}],
            "records": [
                {
                    "domain": "example.com",
                    "name": "cdn",
                    "type": "A",
                    "value": "203.0.113.10",
                    "ttl": 300,
                    "proxied": True,
                }
            ],
        }
    )

    (site,) = decode_snapshot(legacy)

    assert site.visitor_headers == SiteVisitorHeaders()
    assert site.http3_enabled is False


def test_the_firewall_and_the_visitor_headers_stay_separate_blocks():
    """One canonical home each; neither absorbed the other's fields."""
    assert set(SiteFirewall.model_fields).isdisjoint(SiteVisitorHeaders.model_fields)
    assert SitePolicy.model_fields["visitor_headers"].annotation is SiteVisitorHeaders


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
