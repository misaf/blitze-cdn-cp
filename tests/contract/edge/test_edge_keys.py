"""The emitted-key contract: every key a site sends has an owner.

The edge role takes sites as plain dictionaries, so nothing on the edge would
notice a key that no contract declares; this does.
"""

from __future__ import annotations

from support.edge_render import role_spec

from blitzecdn.capabilities.security.contract import SiteFirewall
from blitzecdn.capabilities.serving.contract import SitePolicy, SiteVisitorHeaders

#: What a site is, beside its policy.
_IDENTITY = frozenset({"name", "server_names", "origin_host"})

#: Added by the control plane when it distributes managed certificates.
_DISTRIBUTION = frozenset(
    {
        "certificate_source_path",
        "certificate_key_source_path",
        "certificate_source_sha256",
        "certificate_key_source_sha256",
    }
)


def test_every_emitted_key_belongs_to_the_site_or_its_composed_policy(desired_state):
    owned = _IDENTITY | set(SitePolicy.setting_fields()) | _DISTRIBUTION
    for site in desired_state["blitzecdn_nginx_sites"]:
        unowned = set(site) - owned
        assert not unowned, f"CdnSite emits {sorted(unowned)}, which nothing owns"


def test_a_site_always_carries_its_identity(desired_state):
    for site in desired_state["blitzecdn_nginx_sites"]:
        assert set(site) >= _IDENTITY, site.get("name")


def test_the_role_takes_sites_without_naming_a_policy_setting():
    """A capability adding a setting must not have to edit the core role."""
    sites = role_spec()["blitzecdn_nginx_sites"]
    assert sites["type"] == "list"
    assert sites["elements"] == "dict"
    assert "options" not in sites


def test_nested_blocks_emit_only_their_contract_keys(desired_state):
    for site in desired_state["blitzecdn_nginx_sites"]:
        assert set(site.get("firewall", {})) <= set(SiteFirewall.model_fields)
        assert set(site.get("visitor_headers", {})) <= set(
            SiteVisitorHeaders.model_fields
        )


def test_origin_port_is_not_part_of_the_edge_site_contract():
    assert "origin_port" not in SitePolicy.setting_fields()
    assert "origin_scheme" not in SitePolicy.setting_fields()
