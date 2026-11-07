"""Package nginx boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

from support.paths import SOURCE

from blitzecdn.capabilities.http.contract import HeaderPolicy
from blitzecdn.capabilities.security.contract import SecurityPolicy

# --- the firewall belongs to the security capability, not to core -----------


#: The kinds of rule a site firewall carries, and the vocabulary they are
#: validated against. `SiteFirewall` is the source, so a new kind needs no edit.
def _firewall_rule_kinds() -> frozenset[str]:
    from blitzecdn.capabilities.security.contract import SiteFirewall

    return frozenset(SiteFirewall.model_fields)


_FIREWALL_VOCABULARY = frozenset(
    {"COUNTRY_CODE", "COUNTRY_ALIASES", "ISO_3166_1_ALPHA_2", "HTTP_METHOD"}
)


def _names_and_string_constants(path: Path) -> set[str]:
    """Every identifier and string literal a module actually contains.

    A text search cannot tell `blitzecdn_firewall_ssh_port`, the edge host's SSH
    firewall, from a site's request rules.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg | ast.keyword) and node.arg:
            found.add(node.arg)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
            found.update(node.value.split("."))
    return found


def test_core_knows_no_kind_of_firewall_rule():
    """`core/` names no firewall rule kind and none of its vocabulary.

    A firewall is the security capability's; core holding `allowed_countries` or
    the ISO table would mean generic infrastructure knew one capability's settings.
    """
    forbidden = _firewall_rule_kinds() | _FIREWALL_VOCABULARY
    offenders = [
        f"{path.relative_to(SOURCE)} names {name}"
        for path in sorted((SOURCE / "core").rglob("*.py"))
        for name in sorted(_names_and_string_constants(path) & forbidden)
    ]
    assert offenders == []


#: Where a firewall rule kind may still be named outside its own contract. A new
#: entry is a decision, not an oversight:
#:
#: * `security/cli.py` carries `blitzecdn domain firewall` beside the contract it
#:   edits;
#: * `security/domain.py` names the editable lists for selective firewall
#:   intent.
_FIREWALL_AWARE_MODULES: dict[str, frozenset[str] | None] = {
    "capabilities/security/contract.py": None,
    "capabilities/security/cli.py": None,
    "capabilities/security/domain.py": _firewall_rule_kinds(),
}


def test_only_declared_modules_outside_the_contract_name_a_firewall_rule():
    forbidden = _firewall_rule_kinds() | _FIREWALL_VOCABULARY
    offenders: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE).as_posix()
        if relative not in _FIREWALL_AWARE_MODULES:
            allowed: frozenset[str] = frozenset()
        elif (permitted := _FIREWALL_AWARE_MODULES[relative]) is None:
            continue
        else:
            allowed = permitted
        offenders.extend(
            f"{relative} names {name}"
            for name in sorted(_names_and_string_constants(path) & forbidden - allowed)
        )
    assert offenders == []


def test_the_edge_document_prunes_blocks_by_declaration_not_by_name():
    """`site_to_ansible` asks whether a block opted in, never what it is.

    The source may not name a capability's block, and the behaviour must match
    the edge role and committed fixture: an empty firewall is absent from the
    document, a configured one present, and `visitor_headers` (not
    `OmittedWhenEmpty`) never pruned.
    """
    from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
    from blitzecdn.core.domain.site import CdnSite
    from blitzecdn.core.domain.validation import OmittedWhenEmpty

    source = (SOURCE / "capabilities/dns/adapters/ansible.py").read_text(
        encoding="utf-8"
    )
    mapper = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "site_to_ansible"
    )
    # The docstring is exempt: it may name blocks to explain the pruning.
    body = mapper.body[1:] if ast.get_docstring(mapper) else mapper.body
    assert "firewall" not in "".join(ast.unparse(node) for node in body)

    site = CdnSite(
        name="edge-doc",
        server_names=("edge-doc.example.com",),
        origin_host="198.51.100.10",
    )
    assert isinstance(site.policy[SecurityPolicy].firewall, OmittedWhenEmpty)
    assert not isinstance(site.policy[HeaderPolicy].visitor_headers, OmittedWhenEmpty)

    assert "firewall" not in site_to_ansible(site)
    assert "visitor_headers" in site_to_ansible(site)

    configured = CdnSite(
        name=site.name,
        server_names=site.server_names,
        origin_host=site.origin_host,
        policy=site.policy.applying({"firewall": {"denied_methods": ("DELETE",)}}),
    )
    assert site_to_ansible(configured)["firewall"] == {
        "allow_sources": [],
        "deny_sources": [],
        "allowed_countries": [],
        "denied_countries": [],
        "denied_methods": ["DELETE"],
        "denied_paths": [],
    }
