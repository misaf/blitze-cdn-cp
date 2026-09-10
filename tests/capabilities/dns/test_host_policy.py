import ast
import re

import pytest
from paths import SOURCE
from pydantic import ValidationError

from blitzecdn.capabilities.cache.policy import CacheQueryStringMode
from blitzecdn.capabilities.deployments.domain import (
    DEPLOYMENT_TRANSITIONS,
    TERMINAL_STATUSES,
    DeploymentStatus,
    is_terminal,
    require_transition,
)
from blitzecdn.capabilities.dns.domain import (
    CdnSite,
    DnsRecord,
    Domain,
    DomainPatch,
    SitePolicy,
    derive_hosts,
)
from blitzecdn.capabilities.dns.policy import SiteVisitorHeaders
from blitzecdn.capabilities.http.policy import (
    DEFAULT_PORTS,
    HTTP_PROXY_PORTS,
    HTTPS_PROXY_PORTS,
    HttpScheme,
)
from blitzecdn.capabilities.releases.domain import ReleaseInputs
from blitzecdn.capabilities.security.policy import SiteFirewall
from blitzecdn.capabilities.tls.policy import (
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
    TlsPolicy,
)


def test_site_normalizes_safe_hostnames(site_payload):
    site_payload["server_names"] = ["CDN.Example.COM.", "*.assets.example.com"]
    site_payload["origin_host"] = "Origin.Example.COM."
    site = CdnSite.model_validate(site_payload)
    assert site.server_names == ("cdn.example.com", "*.assets.example.com")
    assert site.origin_host == "origin.example.com"


def test_a_disabled_site_requires_no_optional_implementation(site_payload):
    site = CdnSite.model_validate(
        {
            **site_payload,
            "enabled": False,
            "compression": "brotli",
            "under_attack_mode": True,
        }
    )

    assert site.required_capabilities == frozenset()


@pytest.mark.parametrize(
    "document",
    [
        "{}",
        '{"domains": [], "records": [], "sites": []}',
        '{"schema_version": 1, "domains": [], "records": [], "sites": [], "x": []}',
        '{"schema_version": 1, "domains": {}, "records": [], "sites": []}',
    ],
)
def test_release_inputs_fail_closed_on_incomplete_or_unknown_shapes(document):
    """A stored document that is not this shape is refused, never guessed at.

    ``sites`` is the one worth naming: an input document that carried virtual
    hosts would be a second, older answer beside the state they are derived
    from, and a rollback restoring both would restore a document free to
    disagree with itself.
    """
    with pytest.raises(ValueError):
        ReleaseInputs.decode(document)


def test_release_inputs_fail_closed_on_unknown_schema_versions():
    document = '{"schema_version":999,"domains":[],"records":[],"rules":[]}'
    with pytest.raises(ValueError, match="unsupported release inputs schema version"):
        ReleaseInputs.decode(document)


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
    assert "origin_scheme" not in DomainPatch.model_fields


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


@pytest.mark.parametrize(
    "path",
    [
        "/etc/ssl/x.pem;\n    add_header X-Owned 1;\n    root /etc",
        "/etc/ssl/x.pem; return 200 'owned'",
        "/etc/ssl/x.pem}\nserver {\n    listen 80;",
        "/etc/ssl/two words.pem",
    ],
)
def test_certificate_paths_cannot_carry_nginx_directives(site_payload, path):
    """A path that stays inside the roots can still escape its own directive.

    ``site.conf.j2`` writes ``ssl_certificate {{ path }};`` unquoted, so the
    root and traversal checks alone leave a path free to close the directive
    and open whichever ones its author wants — on every edge the site reaches.
    """
    site_payload["certificate_mode"] = "existing"
    site_payload["certificate_path"] = path
    site_payload["certificate_key_path"] = "/etc/ssl/example/privkey.pem"
    with pytest.raises(ValidationError, match="may contain only"):
        CdnSite.model_validate(site_payload)


def test_zone_and_site_share_one_certificate_path_rule(site_payload):
    """The rule belongs to the TLS contract, so both carriers inherit it.

    Written out on each of them once, which made tightening one and not the
    other a silent way to leave the other open.
    """
    assert (
        Domain.validate_remote_path.__func__
        is CdnSite.validate_remote_path.__func__
        is TlsPolicy.validate_remote_path.__func__
    )


def test_managed_certificate_modes_reject_operator_chosen_paths(site_payload):
    site_payload["certificate_mode"] = "uploaded"
    site_payload["certificate_path"] = "/etc/ssl/elsewhere/fullchain.pem"
    site_payload["certificate_key_path"] = "/etc/ssl/elsewhere/privkey.pem"
    with pytest.raises(ValidationError, match="upload and request endpoints"):
        CdnSite.model_validate(site_payload)


def test_managed_certificate_modes_accept_their_own_paths(site_payload):
    site_payload["certificate_mode"] = "uploaded"
    site_payload["certificate_path"] = "/etc/blitzecdn/tls/example-com/fullchain.pem"
    site_payload["certificate_key_path"] = "/etc/blitzecdn/tls/example-com/privkey.pem"
    assert CdnSite.model_validate(site_payload).certificate_mode == "uploaded"


def _managed_site(**overrides: object) -> dict[str, object]:
    return {
        "name": "example-com",
        "server_names": ["cdn.example.com"],
        "origin_host": "198.51.100.10",
        "certificate_mode": "uploaded",
        "certificate_path": "/etc/blitzecdn/tls/example-com/fullchain.pem",
        "certificate_key_path": "/etc/blitzecdn/tls/example-com/privkey.pem",
        **overrides,
    }


def test_site_patch_cannot_redirect_a_managed_certificate():
    """The escalation path: aim a managed site's cert at an arbitrary file.

    A deploy writes these paths as root, so the site has to be refused before
    it reaches the desired state.
    """
    site = CdnSite.model_validate(_managed_site())
    patch = DomainPatch(certificate_path="/etc/cron.d/blitzecdn")
    with pytest.raises(ValidationError):
        CdnSite.model_validate(
            {**site.model_dump(), **patch.model_dump(exclude_unset=True)}
        )


def test_site_patch_revalidates_the_whole_site():
    """The site is rebuilt from the domain *and* rebuilt from the record, so a
    policy patch lands without touching the origin the edge fetches from."""
    site = CdnSite.model_validate(_managed_site())
    patch = DomainPatch(cache_enabled=False)
    updated = CdnSite.model_validate(
        {**site.model_dump(), **patch.model_dump(exclude_unset=True)}
    )
    assert updated.cache_enabled is False
    assert updated.origin_host == "198.51.100.10"


def test_site_patch_covers_every_shared_policy_field():
    """`DomainPatch` cannot inherit `SitePolicy`, so nothing else keeps it honest.

    Every policy field has to be patchable. One missing is not an error anyone
    sees: the API accepts the request, silently drops the unknown key under
    `extra="forbid"`... or worse, rejects a field an operator can set on
    creation but never change. Adding a field to `SitePolicy` should fail here
    until it is added below it too.
    """
    assert set(SitePolicy.model_fields) <= set(DomainPatch.model_fields), (
        "DomainPatch is missing "
        f"{sorted(set(SitePolicy.model_fields) - set(DomainPatch.model_fields))}. "
        "Add the field to DomainPatch as an optional defaulting to None."
    )


def test_the_patch_cannot_reach_the_field_dns_owns():
    """`server_names` and `origin_host` are both maintained from the records, so
    a patch has no word for them. Everything else about a site is patchable;
    these two things are not."""
    assert "server_names" not in DomainPatch.model_fields
    assert "origin_host" not in DomainPatch.model_fields


def test_every_patchable_policy_field_is_optional():
    """An inherited required field would arrive here with a default and apply
    itself on every unrelated patch."""
    for name in SitePolicy.model_fields:
        assert DomainPatch.model_fields[name].default is None, (
            f"DomainPatch.{name} must default to None so an unset field means "
            "'leave alone' rather than 'reset to this value'"
        )


def test_under_attack_mode_defaults_off_and_is_patchable():
    assert SitePolicy().under_attack_mode is False
    assert DomainPatch(under_attack_mode=True).under_attack_mode is True
    assert (
        CdnSite.model_validate(_managed_site(under_attack_mode=True)).under_attack_mode
        is True
    )


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


def test_every_country_setting_requests_the_geoip_capability(site_payload):
    """The token a detached `blitzecdn-geoip` makes missing, derived here.

    Core owns the derivation because core owns the fields: a controller with
    the capability detached still reads the site back and then refuses to
    deploy it. What the package owns is whether anything answers for the token.
    """
    plain = site_payload | {"compression": "off", "cache_enabled": False}
    header = CdnSite.model_validate(plain | {"visitor_headers": {"ip_country": True}})
    allowed = CdnSite.model_validate(
        plain | {"firewall": {"allowed_countries": ["DE"]}}
    )
    denied = CdnSite.model_validate(plain | {"firewall": {"denied_countries": ["RU"]}})

    assert header.required_capabilities == frozenset({"geoip"})
    # A country rule is a rule and a lookup: two capabilities, named separately
    # so detaching either is reported for what it is.
    assert allowed.required_capabilities == frozenset({"geoip", "security"})
    assert denied.required_capabilities == frozenset({"geoip", "security"})


def test_an_ordinary_site_requests_no_geoip_capability(site_payload):
    """Every firewall rule that is not geographical leaves the lookup out.

    This is the case that must keep working with nothing optional installed
    beyond the firewall itself, so it is asserted rather than assumed.
    """
    site = CdnSite.model_validate(
        site_payload
        | {
            "compression": "off",
            "cache_enabled": False,
            "visitor_headers": {"connecting_ip": True, "ip_country": False},
            "firewall": {
                "deny_sources": ["203.0.113.0/24"],
                "denied_paths": ["/admin"],
            },
        }
    )

    assert site.requires_geoip is False
    assert site.required_capabilities == frozenset({"security"})


def test_capability_requirements_name_the_settings_that_asked(site_payload):
    """Which setting, not only which token.

    "capability 'geoip' is not installed" leaves an operator hunting across
    two unrelated policy blocks. The names are the schema's own, so what the
    message says to change is what a patch would set.
    """
    site = CdnSite.model_validate(
        site_payload
        | {
            "compression": "brotli",
            "visitor_headers": {"ip_country": True},
            "firewall": {"allowed_countries": ["DE"], "denied_countries": ["RU"]},
        }
    )

    assert site.capability_requirements["geoip"] == (
        "firewall.allowed_countries",
        "firewall.denied_countries",
        "visitor_headers.ip_country",
    )
    assert site.capability_requirements["compression"] == ("compression",)
    assert site.capability_requirements["security"] == ("firewall",)
    assert frozenset(site.capability_requirements) == site.required_capabilities


def test_a_disabled_site_names_no_capability_and_no_setting(site_payload):
    """A disabled site converges no server block, so it asks for nothing."""
    site = CdnSite.model_validate(
        site_payload | {"enabled": False, "visitor_headers": {"ip_country": True}}
    )

    assert site.capability_requirements == {}
    assert site.required_capabilities == frozenset()
    # `requires_geoip` is the *edge role's* question and is deliberately not
    # gated on `enabled`: the role asks it of whatever it was handed.
    assert site.requires_geoip is True


def test_a_patch_replaces_the_whole_visitor_header_block():
    """Like the firewall, and for the same reason: partial merges cannot
    express turning the last switch off."""
    site = CdnSite.model_validate(
        _managed_site(visitor_headers={"connecting_ip": True, "ip_country": True})
    )
    patch = DomainPatch(visitor_headers=SiteVisitorHeaders())
    updated = CdnSite.model_validate(
        {**site.model_dump(), **patch.model_dump(exclude_unset=True)}
    )

    assert updated.visitor_headers.connecting_ip is True
    assert updated.visitor_headers.ip_country is False


def _inputs_of(**policy: object) -> ReleaseInputs:
    """Release inputs holding one zone with one proxied hostname in it.

    Sites are not in the inputs — they are derived from them — so a round trip
    has to go through the zone and the record that produce one. The policy
    lives on the zone and the origin on the record; what survives JSON on the
    way to a run and back from a rollback is, between them, the site.
    """
    zone = Domain.model_validate({"name": "example.com", **policy})
    record = DnsRecord(
        domain="example.com", name="cdn", value="198.51.100.10", proxied=True
    )
    return ReleaseInputs.of([zone], [record], [])


def _derived(inputs: ReleaseInputs) -> list[CdnSite]:
    restored = ReleaseInputs.decode(inputs.encode())
    return derive_hosts(
        list(restored.domains), list(restored.rules), list(restored.records)
    )


def test_visitor_headers_survive_an_inputs_round_trip():
    (site,) = _derived(
        _inputs_of(visitor_headers={"connecting_ip": False, "ip_country": True})
    )

    assert site.visitor_headers.connecting_ip is False
    assert site.visitor_headers.ip_country is True
    assert site.requires_geoip is True


def test_http3_survives_an_inputs_round_trip():
    (site,) = _derived(
        _inputs_of(
            ssl_mode="flexible",
            http3_enabled=True,
            certificate_mode="existing",
            certificate_path="/etc/ssl/certs/edge.pem",
            certificate_key_path="/etc/ssl/private/edge.key",
        )
    )
    assert site.http3_enabled is True


def test_http3_changes_input_identity_only_when_the_value_changes():
    tls = {
        "ssl_mode": "flexible",
        "certificate_mode": "existing",
        "certificate_path": "/etc/ssl/certs/edge.pem",
        "certificate_key_path": "/etc/ssl/private/edge.key",
    }
    baseline = _inputs_of(**tls, http3_enabled=False).digest
    assert _inputs_of(**tls, http3_enabled=False).digest == baseline
    assert _inputs_of(**tls, http3_enabled=True).digest != baseline


def test_the_firewall_and_the_visitor_headers_stay_separate_blocks():
    """One canonical home each; neither absorbed the other's fields."""
    assert set(SiteFirewall.model_fields).isdisjoint(SiteVisitorHeaders.model_fields)
    assert SitePolicy.model_fields["visitor_headers"].annotation is SiteVisitorHeaders


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


#: Site settings an operator cannot reach from `blitzecdn site`, and why.
#:
#: The three certificate fields move together under
#: `CdnSite.validate_certificate_pair`, and two of the four modes —
#: `uploaded` and `requested` — are refused to anything but the upload and
#: request endpoints, which own the paths under the managed TLS root. A CLI
#: command that set them would be rejected by the model it was writing to.
_SET_ELSEWHERE = frozenset(
    {"certificate_mode", "certificate_path", "certificate_key_path"}
)


def test_every_patchable_field_can_be_reached_from_the_command_line():
    """The fourth register of the site document, and the one nothing checked.

    A site setting exists in the domain model, in `DomainPatch`, in the API model
    and in `blitzecdn_nginx`'s argument spec, and all four are held in step —
    `_assert_patch_covers_policy` at import, the Ansible spec in
    `tests/contract/`. The CLI was the fifth and had no such check, so a
    setting could reach every one of those and still have no verb: `site
    cache-query-string` set the mode of a cache the command line could not turn
    on, and neither TTL nor either origin-identity field was reachable at all.

    `_HELP_ORDER` guards the other direction — a command with nowhere to sit is
    an import error — which is why the gap was invisible: nothing was missing
    from a list, a list was missing from nothing.

    Matching on the field *name* anywhere in the group is deliberately loose.
    A command that names a field and edits a different one is a bug this cannot
    see; a field no command mentions at all is the failure that actually
    happened, and it is worth one cheap assertion.
    """
    named: set[str] = set()
    for path in (SOURCE / "capabilities/dns/cli").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg:
                named.add(node.arg)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                named.add(node.value)

    unreachable = sorted(set(DomainPatch.model_fields) - named - _SET_ELSEWHERE)
    assert unreachable == [], (
        f"no `blitzecdn domain` command mentions {unreachable}. Add one, or add "
        "the field to _SET_ELSEWHERE with the reason it is not an operator's "
        "to set."
    )
    assert set(DomainPatch.model_fields) >= _SET_ELSEWHERE, (
        "_SET_ELSEWHERE names a field that no longer exists"
    )
