"""Every edge-visible setting has exactly one owning capability.

The ownership rule, checked where it actually bites: on the document the
control plane hands Ansible, and on the template that renders it. The layering
tests prove the *packages* are arranged correctly; nothing there would notice a
field two capabilities both declare, a field no capability declares, or a
setting that reaches the desired state and is then never rendered.

The last one is the quiet failure that matters: an operator sets a switch, the
API accepts it, the deployment converges, and the edge behaves exactly as it
did before.
"""

from __future__ import annotations

import re

from paths import REPO_ROOT

from blitzecdn.core.ansible.mapping import site_to_ansible
from blitzecdn.features.compression.policy import CompressionPolicy
from blitzecdn.features.http.policy import ProtocolPolicy
from blitzecdn.features.security.policy import SecurityPolicy, SiteFirewall
from blitzecdn.features.sites.domain import CdnSite, SitePolicy
from blitzecdn.features.sites.policy import (
    CachePolicy,
    HeaderPolicy,
    OriginPolicy,
    SiteVisitorHeaders,
)
from blitzecdn.features.tls.policy import TlsPolicy

_ROLE = REPO_ROOT / "ansible/roles/blitzecdn_nginx"
_TEMPLATE = (_ROLE / "templates/site.conf.j2").read_text(encoding="utf-8")
#: The whole role. A site setting reaches the edge through the template that
#: renders it *or* through the tasks that act on it — `certificate_mode` is the
#: second kind: it selects which material `main.yml` copies and what
#: `validate.yml` insists on, and never appears in a directive.
_ROLE_TEXT = "".join(
    path.read_text(encoding="utf-8")
    for path in sorted(_ROLE.rglob("*"))
    if path.is_file()
)

#: Which capability's policy class declares which slice of the flat site
#: document. The class is the source of truth — this names the owner, and the
#: fields come from the model, so a knob cannot be added without landing here.
_CAPABILITY_POLICIES = {
    "compression": CompressionPolicy,
    "http": ProtocolPolicy,
    "security": SecurityPolicy,
    "tls": TlsPolicy,
    "sites": (CachePolicy, HeaderPolicy, OriginPolicy),
}

#: Fields on the site model that belong to no capability policy because they
#: are the site's *identity* rather than its configuration.
_SITE_IDENTITY = {"name", "server_names", "origin_host", "enabled"}


def _owned_fields() -> dict[str, str]:
    """Every policy field, mapped to the capability that declares it."""
    owners: dict[str, str] = {}
    for capability, policies in _CAPABILITY_POLICIES.items():
        classes = policies if isinstance(policies, tuple) else (policies,)
        for policy in classes:
            for field in policy.model_fields:
                assert field not in owners, (
                    f"{field} is declared by both {owners.get(field)} and {capability}"
                )
                owners[field] = capability
    return owners


def test_every_site_policy_field_has_exactly_one_owning_capability():
    owners = _owned_fields()

    assert set(SitePolicy.model_fields) - _SITE_IDENTITY == set(owners)
    assert set(CdnSite.model_fields) - _SITE_IDENTITY == set(owners)


def test_the_capability_owning_each_field_is_the_one_it_reads_like():
    """Spot-checked by hand, so a silent re-parenting fails rather than passes."""
    owners = _owned_fields()

    assert owners["compression"] == "compression"
    assert owners["http3_enabled"] == "http"
    assert owners["under_attack_mode"] == "security"
    assert owners["firewall"] == "security"
    assert owners["ssl_mode"] == "tls"
    assert owners["minimum_tls_version"] == "tls"
    assert owners["certificate_mode"] == "tls"
    assert owners["always_use_https"] == "tls"
    assert owners["cache_enabled"] == "sites"
    assert owners["visitor_headers"] == "sites"
    assert owners["origin_sni"] == "sites"


def test_the_nginx_template_reads_only_fields_a_capability_owns():
    """`item.<x>` in the template is a setting somebody had better own."""
    referenced = set(re.findall(r"item\.([a-z_0-9]+)", _TEMPLATE))
    known = set(_owned_fields()) | _SITE_IDENTITY

    assert referenced <= known, referenced - known


def test_every_edge_visible_setting_reaches_the_nginx_template():
    """A switch the desired state carries and the template never reads is inert.

    `ssl_automatic_mode` is the one deliberate exception: whether the control
    plane may raise a site's encryption mode for it is a decision taken here,
    and the edge only ever sees the `ssl_mode` it produced. Named so a second
    cannot join it silently.
    """
    site = CdnSite(
        name="alpha",
        server_names=("alpha.example.com",),
        origin_host="198.51.100.10",
    )
    document = set(site_to_ansible(site)) | {"firewall"}
    referenced = set(re.findall(r"item\.([a-z_0-9]+)", _ROLE_TEXT))

    assert document - referenced == {"ssl_automatic_mode"}


def test_the_nested_blocks_are_read_through_their_owning_capability():
    """`firewall` and `visitor_headers` are replaced wholesale, so are nested."""
    firewall = set(re.findall(r"fw\.([a-z_0-9]+)", _TEMPLATE))
    headers = set(re.findall(r"vh\.([a-z_0-9]+)", _TEMPLATE))

    assert firewall == set(SiteFirewall.model_fields)
    assert headers == set(SiteVisitorHeaders.model_fields)
