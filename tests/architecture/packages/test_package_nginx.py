"""Package nginx boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

from paths import SOURCE

# --- the firewall belongs to the security capability, not to core -----------


#: The six kinds of rule a site firewall carries, and the vocabulary they are
#: validated against. `SiteFirewall` is the source of the first six, so a
#: seventh rule kind lands here without this list being edited.
def _firewall_rule_kinds() -> frozenset[str]:
    from blitzecdn.capabilities.security.policy import SiteFirewall

    return frozenset(SiteFirewall.model_fields)


_FIREWALL_VOCABULARY = frozenset(
    {"COUNTRY_CODE", "COUNTRY_ALIASES", "ISO_3166_1_ALPHA_2", "HTTP_METHOD"}
)


def _names_and_string_constants(path: Path) -> set[str]:
    """Every identifier and string literal a module actually contains.

    Text search would answer differently: `core/validation.py` names
    `blitzecdn_firewall_ssh_port`, which is the *edge host's* SSH firewall — a
    different concept that reaches Ansible from the edge record, and one that
    a grep for "firewall" cannot tell apart from a site's request rules.
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
    """`core/` names none of it — not a rule kind, not the vocabulary.

    The rule the whole extraction rests on, stated where it can fail. A firewall
    is the security capability's, and `blitzecdn-security` is a wheel an
    operator can leave out; core carrying `allowed_countries` or the ISO table
    would mean the control plane's generic infrastructure had memorised the
    settings of something detachable. Two leaks lived here and are gone: the
    country and HTTP-method tables in `core/validation.py`, whose only consumer
    was the security contract, and `if site.firewall.empty` in
    `capabilities/dns/adapters/ansible.py`, which named one capability's
    block inside a generic adapter.
    """
    forbidden = _firewall_rule_kinds() | _FIREWALL_VOCABULARY
    offenders = [
        f"{path.relative_to(SOURCE)} names {name}"
        for path in sorted((SOURCE / "core").rglob("*.py"))
        for name in sorted(_names_and_string_constants(path) & forbidden)
    ]
    assert offenders == []


#: Where a firewall rule kind may still be named outside its own contract, and
#: why. This list *is* the "remaining core knowledge" section of the
#: extraction: it is short, every entry is a deliberate contract, and a new
#: entry is a decision rather than an oversight.
#:
#: * `api/models.py` holds the published *resource* shapes, which restate the
#:   fields rather than re-export the contract, because what a client is
#:   shown is a decision separate from what a policy happens to hold;
#: * `dns/cli/security.py` carries `blitzecdn domain firewall`, and is only the
#:   commands for that one contract — it was the whole 572-line `sites/cli.py`,
#:   which meant every unrelated command shared an exemption written for two of
#:   them;
#: `sites/domain.py` used to be a fourth entry, permitted to name the two
#: *country* settings so it could derive the `geoip` token. It derives nothing
#: now — `SecurityPolicy` declares the token beside the rule that needs it — so
#: the exception is gone rather than documented, which is the outcome worth
#: having for a list whose whole purpose is to stay short.
_FIREWALL_AWARE_MODULES: dict[str, frozenset[str] | None] = {
    "capabilities/security/policy.py": None,
    "capabilities/dns/api/models.py": None,
    "capabilities/dns/cli/security.py": None,
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

    Both halves matter. The source may not name a capability's block, and the
    behaviour must still be the one the edge role and the committed contract
    fixture were written against: an empty firewall is *absent* from the
    document rather than present and empty, a configured one is there, and
    `visitor_headers` — which is not `OmittedWhenEmpty` — is never pruned,
    because a block that has never been configured is not the same as one whose
    switches are off.
    """
    from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
    from blitzecdn.capabilities.dns.domain import CdnSite
    from blitzecdn.core.domain.validation import OmittedWhenEmpty

    source = (SOURCE / "capabilities/dns/adapters/ansible.py").read_text(
        encoding="utf-8"
    )
    mapper = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "site_to_ansible"
    )
    # The docstring is exempt: it explains what the branch used to be, which is
    # the record of why the pruning is written this way.
    body = mapper.body[1:] if ast.get_docstring(mapper) else mapper.body
    assert "firewall" not in "".join(ast.unparse(node) for node in body)

    site = CdnSite(
        name="edge-doc",
        server_names=("edge-doc.example.com",),
        origin_host="198.51.100.10",
    )
    assert isinstance(site.firewall, OmittedWhenEmpty)
    assert not isinstance(site.visitor_headers, OmittedWhenEmpty)

    assert "firewall" not in site_to_ansible(site)
    assert "visitor_headers" in site_to_ansible(site)

    configured = site.model_copy(
        update={"firewall": type(site.firewall)(denied_methods=("DELETE",))}
    )
    assert site_to_ansible(configured)["firewall"] == {
        "allow_sources": [],
        "deny_sources": [],
        "allowed_countries": [],
        "denied_countries": [],
        "denied_methods": ["DELETE"],
        "denied_paths": [],
    }
