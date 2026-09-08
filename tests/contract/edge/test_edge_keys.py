"""The emitted-key contract: what the model sends, what the role declares."""

from __future__ import annotations

import pytest

# The nginx role renders from its own defaults *and* the shared edge runtime
# contract, so the loader that builds that namespace is shared with the other
# contract-test modules rather than reimplemented here.
from edge_render_support import (
    _role_spec,
)

from blitzecdn.capabilities.cache.policy import CacheQueryStringMode
from blitzecdn.capabilities.compression.policy import CompressionMode
from blitzecdn.capabilities.dns.domain import (
    SitePolicy,
)
from blitzecdn.capabilities.dns.policy import SiteVisitorHeaders
from blitzecdn.capabilities.http.policy import (
    MaxUploadSize,
)
from blitzecdn.capabilities.security.policy import SiteFirewall
from blitzecdn.capabilities.tls.policy import (
    CertificateMode,
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)


def test_every_emitted_key_is_declared_by_the_role(desired_state):
    declared = set(_role_spec()["blitzecdn_nginx_sites"]["options"])
    # The control plane adds these when distributing managed certificates.
    declared |= {"certificate_source_path", "certificate_key_source_path"}
    for site in desired_state["blitzecdn_nginx_sites"]:
        undeclared = set(site) - declared
        assert not undeclared, (
            f"CdnSite emits {sorted(undeclared)}, which the local "
            "roles/blitzecdn_nginx/meta/argument_specs.yml does not declare."
        )


def test_required_keys_are_always_emitted(desired_state):
    options = _role_spec()["blitzecdn_nginx_sites"]["options"]
    required = {name for name, spec in options.items() if (spec or {}).get("required")}
    for site in desired_state["blitzecdn_nginx_sites"]:
        missing = required - set(site)
        assert not missing, (
            f"site {site.get('name')!r} omits required {sorted(missing)}"
        )


def test_nginx_role_accepts_only_the_current_ssl_policy():
    options = _role_spec()["blitzecdn_nginx_sites"]["options"]
    assert options["ssl_mode"]["required"] is True
    assert "origin_scheme" not in options


@pytest.mark.parametrize(
    ("field", "enum"),
    [
        ("ssl_mode", SslMode),
        ("ssl_automatic_mode", SslAutomaticMode),
        ("minimum_tls_version", MinimumTlsVersion),
        ("certificate_mode", CertificateMode),
        ("cache_query_string_mode", CacheQueryStringMode),
        ("compression", CompressionMode),
        ("max_upload_size", MaxUploadSize),
    ],
)
def test_role_choices_cover_every_domain_value(field, enum):
    """A new enum member must not reach a role that rejects it."""
    declared = set(_role_spec()["blitzecdn_nginx_sites"]["options"][field]["choices"])
    assert {member.value for member in enum} <= declared, (
        f"{enum.__name__} has values the role's {field} choices do not allow"
    )


def test_every_emitted_firewall_key_is_declared_by_the_role(desired_state):
    """The outer check only sees top-level keys; the firewall is nested.

    Role argument validation rejects an undeclared suboption, so a new
    ``SiteFirewall`` field reaching an older role fails the deploy rather than
    being ignored — which is the right failure, but only if it is caught here
    first.
    """
    declared = set(
        _role_spec()["blitzecdn_nginx_sites"]["options"]["firewall"]["options"]
    )
    assert set(SiteFirewall.model_fields) == declared, (
        "SiteFirewall and the role's firewall suboptions disagree: "
        f"{sorted(set(SiteFirewall.model_fields) ^ declared)}"
    )
    for site in desired_state["blitzecdn_nginx_sites"]:
        assert set(site.get("firewall", {})) <= declared


def test_the_role_defaults_agree_with_the_domain_defaults():
    """An edge upgraded ahead of its controller must behave the same.

    The role's suboption defaults apply when an older control plane sends no
    visitor_headers at all, so they have to be the values the domain would have
    sent.
    """
    options = _role_spec()["blitzecdn_nginx_sites"]["options"]["visitor_headers"]
    assert options.get("required", False) is False
    for name, field in SiteVisitorHeaders.model_fields.items():
        assert options["options"][name]["default"] == field.default


def test_origin_port_is_not_part_of_the_edge_site_contract():
    options = _role_spec()["blitzecdn_nginx_sites"]["options"]
    assert "origin_port" not in SitePolicy.model_fields
    assert "origin_port" not in options
