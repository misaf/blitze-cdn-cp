"""The shared edge runtime contract.

``blitzecdn_nginx``, ``blitzecdn_edge_stack`` and ``blitzecdn_firewall``
converge one machine and need the same answers: where files live, which ports
it listens on, where health is read. One contract, read by all three.
"""

import re
from pathlib import Path
from typing import Any

import yaml
from support.contracts import (
    PROJECT_DIR,
    ROLES_DIR,
    _runtime_defaults,
    contributed_roles,
)

#: The roles that converge an edge and share its runtime.
EDGE_ROLES = ("blitzecdn_nginx", "blitzecdn_edge_stack", "blitzecdn_firewall")


#: The references across those roles that are not contract members.
#:
#: blitzecdn_nginx_config_test_image is a parameter blitzecdn_edge_stack passes
#: to validate against a new image, declared by blitzecdn_nginx.
#: blitzecdn_nginx_listeners_claimed is an output the Nginx role publishes for
#: health.yml.
SIBLING_EXCEPTIONS = {
    "blitzecdn_nginx_config_test_image",
    "blitzecdn_nginx_listeners_claimed",
}


def test_no_edge_role_reads_another_edge_roles_variables():
    """A role never reads a sibling role's variables.

    Such a read couples the roles invisibly and lets one value silently disagree
    with another. Everything shared is blitzecdn_edge_runtime's.
    """
    for role in EDGE_ROLES:
        others = [f"{other}_" for other in EDGE_ROLES if other != role]
        for source in sorted((ROLES_DIR / role).rglob("*")):
            if source.suffix not in {".yml", ".j2"} or not source.is_file():
                continue
            document = source.read_text(encoding="utf-8")
            for line in document.splitlines():
                # Prose is where these roles explain themselves to each other,
                # and naming a sibling there is the point.
                if line.lstrip().startswith("#"):
                    continue
                for prefix in others:
                    for word in re.findall(rf"{re.escape(prefix)}[a-z0-9_]+", line):
                        assert word in SIBLING_EXCEPTIONS, (
                            f"{source.relative_to(PROJECT_DIR)} reads {word}, "
                            "which belongs to another edge role. Shared runtime "
                            "values are blitzecdn_edge_runtime's."
                        )


def test_every_edge_role_declares_the_contract_it_reads():
    """An undeclared contract is an undefined-variable error mid-converge.

    Declared, a play missing blitzecdn_edge fails argument validation first.
    """
    for role in EDGE_ROLES:
        spec = yaml.safe_load(
            (ROLES_DIR / role / "meta/argument_specs.yml").read_text(encoding="utf-8")
        )["argument_specs"]["main"]["options"]
        assert spec["blitzecdn_edge_runtime"]["required"] is True, role
        assert spec["blitzecdn_edge_runtime"]["type"] == "dict", role


def _contract_readers() -> dict[str, Path]:
    """Every role that legitimately reads the shared runtime contract.

    Core's three edge roles and every contributed role: a capability converging
    an edge reads `blitzecdn_edge_runtime` exactly as core's roles do.
    """
    return {
        **{role: ROLES_DIR / role for role in EDGE_ROLES},
        **contributed_roles(),
    }


def test_the_contract_holds_no_value_only_one_role_uses():
    """Every member has to be read by at least two roles that converge an edge.

    Otherwise the contract becomes a dumping ground. Nginx policy stays in
    blitzecdn_nginx; the health timeout and rollback record in
    blitzecdn_edge_stack. A capability's role counts: blitzecdn_edge_stack and the
    cache role both own the cache directory as the image's worker uid.
    """
    runtime = _runtime_defaults()["blitzecdn_edge_runtime"]
    sources = {
        role: "\n".join(
            source.read_text(encoding="utf-8")
            for source in sorted(directory.rglob("*"))
            if source.suffix in {".yml", ".j2"} and source.is_file()
        )
        for role, directory in _contract_readers().items()
    }

    def members(prefix: str, value: Any):
        if isinstance(value, dict):
            for key, item in value.items():
                yield from members(f"{prefix}.{key}", item)
        else:
            yield prefix

    for member in members("blitzecdn_edge_runtime", runtime):
        readers = [role for role, text in sources.items() if member in text]
        assert len(readers) >= 2, (
            f"{member} is read only by {readers or 'nothing'}. A value one role "
            "owns belongs in that role's defaults, not in the shared contract."
        )


def test_the_shared_runtime_is_defined_in_exactly_one_place():
    """One authoritative value, which is the whole point of the exercise."""
    duplicated = (
        "/var/cache/nginx/blitzecdn",
        "/var/lib/blitzecdn/acme",
        "/var/lib/blitzecdn/empty",
        "/stub_status",
        "8090",
        "2052, 2082, 2086, 2095",
        "2053, 2083, 2087, 2096",
    )
    for role in EDGE_ROLES:
        defaults = (ROLES_DIR / role / "defaults/main.yml").read_text(encoding="utf-8")
        body = "\n".join(
            line for line in defaults.splitlines() if not line.lstrip().startswith("#")
        )
        for literal in duplicated:
            assert literal not in body, (
                f"{role} restates {literal!r}, which blitzecdn_edge owns. Two "
                "copies of a runtime value agree until the day one is changed."
            )
