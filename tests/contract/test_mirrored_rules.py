"""Every rule spelled in more than one place, and the test that holds the copies.

Some rules have to be written twice: once in Python and once in a Jinja
template, a role's defaults, or the inventory plugin, which Ansible loads by
path and which cannot import the package. Where a value can be computed once
and handed over as data, it is: a path rule's regex reaches ``site.conf.j2`` as
the location's ``pattern``, built by ``path_pattern``, and is no longer listed
here.

What remains is listed below, each pair with the test that fails when the
copies drift. A source file marks its side of a pair with a note reading
"Mirrors ...", and a file carrying such a note that no entry names fails here,
so a new copy cannot go unlisted, and an entry cannot go without a test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest
from support.paths import REPO_ROOT, SOURCE


@dataclass(frozen=True)
class Mirror:
    #: What the copies agree on.
    rule: str
    #: The files holding a copy, relative to ``src/blitzecdn``.
    sides: tuple[str, ...]
    #: ``path::test_name`` for each test pinning them, relative to the repo root.
    pinned_by: tuple[str, ...]


MIRRORS = (
    Mirror(
        rule="allowed-path alternation: a '/'-terminated path is a prefix",
        sides=(
            "capabilities/security/contract.py",
            "capabilities/security/nginx/security-access.conf.j2",
            "capabilities/security/nginx/security-http.conf.j2",
        ),
        pinned_by=(
            "tests/contract/edge/test_edge_firewall.py"
            "::test_the_allowed_paths_rule_is_the_contracts_pattern",
            "tests/contract/edge/test_edge_rate_limit.py"
            "::test_the_limited_paths_rule_is_the_contracts_pattern",
            "tests/contract/edge/test_edge_security_parity.py"
            "::test_allowed_path_boundaries_mean_the_same_on_both_sides",
            "tests/contract/edge/test_edge_security_parity.py"
            "::test_generated_allowed_paths_mean_the_same_on_both_sides",
            "tests/contract/edge/test_edge_security_parity.py"
            "::test_a_template_that_drops_the_prefix_rule_is_caught",
        ),
    ),
    Mirror(
        rule="rate-limit shared zone names",
        sides=(
            "capabilities/security/contract.py",
            "capabilities/security/nginx/security-http.conf.j2",
            "capabilities/security/nginx/security-access.conf.j2",
        ),
        pinned_by=(
            "tests/contract/edge/test_edge_rate_limit.py"
            "::test_every_zone_a_site_reads_is_declared",
            "tests/contract/edge/test_edge_security_parity.py"
            "::test_every_rung_names_its_own_zone_on_both_sides",
            "tests/contract/edge/test_edge_security_parity.py"
            "::test_generated_sites_declare_exactly_the_zones_they_read",
            "tests/contract/edge/test_edge_security_parity.py"
            "::test_a_template_that_names_zones_differently_is_caught",
        ),
    ),
    Mirror(
        rule="the ladder of rates a site may limit to",
        sides=(
            "capabilities/security/contract.py",
            "capabilities/security/ansible/roles/blitzecdn_security/tasks/main.yml",
        ),
        pinned_by=(
            "tests/capabilities/security/test_security_role_contracts.py"
            "::test_the_role_holds_rates_to_the_contracts_ladder",
        ),
    ),
    Mirror(
        rule="the edge group's name",
        sides=(
            "capabilities/edges/domain/edge.py",
            "assets/ansible/plugins/inventory/blitzecdn.py",
            "core/ansible/rendering.py",
        ),
        pinned_by=(
            "tests/contract/test_inventory.py"
            "::test_the_plugins_reserved_names_match_the_domains",
            "tests/contract/test_inventory.py"
            "::test_the_controller_renders_with_the_edge_groups_variables",
        ),
    ),
    Mirror(
        rule="settings the inventory never publishes at host precedence",
        sides=(
            "core/domain/validation.py",
            "assets/ansible/plugins/inventory/blitzecdn.py",
        ),
        pinned_by=(
            "tests/contract/test_inventory.py"
            "::test_the_plugins_reserved_names_match_the_domains",
        ),
    ),
    Mirror(
        rule="an edge's inventory host variables",
        sides=(
            "capabilities/edges/adapters/ansible.py",
            "assets/ansible/plugins/inventory/blitzecdn.py",
        ),
        pinned_by=(
            "tests/contract/test_inventory.py"
            "::test_the_plugin_publishes_what_the_model_renders",
        ),
    ),
    Mirror(
        rule="the cache key, built by the edge and recomputed by a purge",
        sides=(
            "capabilities/cache/nginx/cache-upstream.conf.j2",
            "capabilities/cache/ansible/roles/blitzecdn_cache_purge/tasks/main.yml",
        ),
        pinned_by=(
            "tests/capabilities/cache/test_cache_role_contracts.py"
            "::test_purge_covers_every_cache_key_variant_the_site_template_can_produce",
            "tests/capabilities/cache/test_cache_role_contracts.py"
            "::test_named_purge_computes_the_same_port_cache_key",
        ),
    ),
    Mirror(
        rule="public proxy ports",
        sides=(
            "capabilities/http/contract.py",
            "assets/ansible/roles/blitzecdn_edge/defaults/main.yml",
        ),
        pinned_by=(
            "tests/contract/edge/test_edge_listeners.py"
            "::test_public_ports_match_cloudflare_and_the_firewall",
        ),
    ),
    Mirror(
        rule="paths the edge teardown removes",
        sides=("assets/ansible/roles/blitzecdn_edge_teardown/defaults/main.yml",),
        pinned_by=(
            "tests/contract/roles/test_edge_runtime.py"
            "::test_teardown_removes_the_paths_the_converging_roles_wrote",
        ),
    ),
    Mirror(
        rule="files the hardening teardown removes",
        sides=(
            "capabilities/hardening/ansible/roles/"
            "blitzecdn_hardening_teardown/defaults/main.yml",
        ),
        pinned_by=(
            "tests/capabilities/hardening/test_hardening_role_contracts.py"
            "::test_the_roles_that_write_these_files_and_the_role_that_removes_them_agree",
        ),
    ),
    Mirror(
        rule="the drop-in the resolver teardown removes",
        sides=(
            "capabilities/resolver/ansible/roles/"
            "blitzecdn_resolver_teardown/defaults/main.yml",
        ),
        pinned_by=(
            "tests/capabilities/resolver/test_resolver_role_contracts.py"
            "::test_the_role_that_writes_the_drop_in_and_the_role_that_removes_it_agree",
        ),
    ),
)

#: How a source file marks its side of a pair.
_NOTE = re.compile(r"\bMirrors\b")


@pytest.mark.parametrize("mirror", MIRRORS, ids=lambda mirror: mirror.rule)
def test_every_side_of_a_pair_exists(mirror: Mirror) -> None:
    missing = [side for side in mirror.sides if not (SOURCE / side).is_file()]

    assert not missing, missing


@pytest.mark.parametrize("mirror", MIRRORS, ids=lambda mirror: mirror.rule)
def test_every_pair_is_pinned_by_a_test_that_exists(mirror: Mirror) -> None:
    """Read rather than imported: some live under capabilities not installed."""
    assert mirror.pinned_by, f"{mirror.rule} has no test holding its copies"
    for pin in mirror.pinned_by:
        path, _, name = pin.partition("::")
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        assert re.search(rf"^def {re.escape(name)}\(", text, re.MULTILINE), pin


def test_every_mirrors_note_names_a_listed_pair() -> None:
    """A file that says it mirrors something is a side some entry must name."""
    listed = {side for mirror in MIRRORS for side in mirror.sides}
    noted = {
        path.relative_to(SOURCE).as_posix()
        for path in SOURCE.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".j2", ".yml", ".yaml"}
        and _NOTE.search(path.read_text(encoding="utf-8"))
    }

    assert noted <= listed, sorted(noted - listed)
