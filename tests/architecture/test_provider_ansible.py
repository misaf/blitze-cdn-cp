"""Package ansible boundaries."""

from __future__ import annotations

from pathlib import Path

import yaml
from support.paths import REPO_ROOT, SOURCE

#: Variables that are the interface between roles rather than one role's own
#: input, so they are the exception to the prefix rule below.
#:
#: `blitzecdn_edge_runtime` is the resolved edge contract many roles read.
#: `blitzecdn_nginx_sites` is the site document several roles project from.
#: `blitzecdn_nginx_http3_listener_owner` tells `nginx` which role opened the
#: QUIC listener. `blitzecdn_release_digest` is the compiled release identity.
_SHARED_FLEET_VARIABLES = frozenset(
    {
        "blitzecdn_edge_runtime",
        "blitzecdn_release_digest",
        "blitzecdn_nginx_sites",
        "blitzecdn_nginx_http3_listener_owner",
    }
)


def _role_specifications(root: Path) -> list[tuple[str, dict[str, object]]]:
    """Every role under a `roles/` directory, with its declared options."""
    found = []
    for spec in sorted(root.glob("*/meta/argument_specs.yml")):
        document = yaml.safe_load(spec.read_text(encoding="utf-8")) or {}
        options: dict[str, object] = {}
        for entry in (document.get("argument_specs") or {}).values():
            options.update(entry.get("options") or {})
        found.append((spec.parent.parent.name, options))
    return found


def test_a_roles_variables_carry_its_name():
    """Ansible's own convention: a role's variables carry its name.

    Variables are global to a play, so the prefix keeps each role's inputs out of
    one shared namespace. The exceptions above are the interface between roles;
    growing that list should be a decision.
    """
    roots = [(CORE_ANSIBLE / "roles", "blitzecdn")]
    roots += [
        (roles, "")
        for roles in SOURCE.rglob("ansible/roles")
        if roles != CORE_ANSIBLE / "roles"
    ]
    offenders = [
        f"{role} declares {option}, which is neither {role}_* nor shared"
        for root, _ in roots
        if root.is_dir()
        for role, options in _role_specifications(root)
        for option in sorted(options)
        if option not in _SHARED_FLEET_VARIABLES
        and option != role
        and not option.startswith(f"{role}_")
    ]
    assert offenders == []


# ----------------------------------------------------------------------
# Ansible ownership
#
# A capability's roles, templates and fleet settings live with the capability.
# Three rules: no capability's name in core's Ansible, no capability's role in
# core's tree, and a contributed role that the edge play really runs.
# ----------------------------------------------------------------------

#: The repository-level Ansible tree, which is the platform's alone: base host,
#: container engine, firewall, edge runtime contract, `blitzecdn_nginx`, and the
#: slots capabilities fill.
CORE_ANSIBLE = REPO_ROOT / "src/blitzecdn/assets/ansible"


#: Words that name a capability's implementation rather than a setting core
#: renders: the GeoIP2 module and its updater, the MaxMind account, njs, and the
#: resolver drop-in directory. `$blitzecdn_country` and `under_attack_mode` are
#: not here: they are site settings core renders from desired state.
CAPABILITY_IMPLEMENTATION_WORDS = (
    "resolved.conf.d",
    "maxmind",
    "geolite",
    "geoip2",
    "geoipupdate",
    "geoip_enabled",
    "js_import",
    "under_attack_secret",
    "under_attack_enabled",
)


#: There are no exemptions. The modules an edge loads are declared by the
#: capabilities that need them and resolved by
#: `blitzecdn.core.features.resolution.resolve_edge_modules`.
CAPABILITY_WORD_EXEMPTIONS: frozenset[str] = frozenset()


def _core_ansible_files() -> list[Path]:
    return sorted(
        path
        for path in CORE_ANSIBLE.rglob("*")
        if path.is_file()
        and path.suffix in {".yml", ".yaml", ".j2", ".cfg", ".sh"}
        and "__pycache__" not in path.parts
    )


def test_core_ansible_names_no_capabilitys_implementation():
    """The platform tree provisions the platform, and stops there.

    A grep, because the failure is textual: a task fetching a MaxMind database or
    a template emitting `js_import` belongs to a capability wherever it is
    written. Comments are exempt, so a file may explain why something is absent.
    """
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number} names {word!r}"
        for path in _core_ansible_files()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if not line.lstrip().startswith(("#", ";"))
        and str(path.relative_to(CORE_ANSIBLE)) not in CAPABILITY_WORD_EXEMPTIONS
        for word in CAPABILITY_IMPLEMENTATION_WORDS
        if word in line.lower()
    ]
    assert offenders == [], (
        "core's Ansible tree carries an optional capability's implementation; "
        "it belongs in that distribution's own roles/ directory: " + str(offenders)
    )


def test_no_capability_owned_role_survives_in_the_core_tree():
    """The same rule as a directory listing.

    `resolve_role_search_path` puts core's directory first, so a stale copy of a
    capability's role there would win.
    """
    contributed = {
        directory.name
        for roles in SOURCE.rglob("ansible/roles")
        if roles != CORE_ANSIBLE / "roles"
        for directory in roles.iterdir()
        if directory.is_dir()
    }
    core = {
        directory.name
        for directory in (CORE_ANSIBLE / "roles").iterdir()
        if directory.is_dir()
    }

    assert contributed, "no optional distribution ships an Ansible role at all"
    assert contributed & core == set(), (
        "a role is shipped by both core and a package; core's directory is "
        f"searched first, so the package's copy would never run: {contributed & core}"
    )


def test_core_nginx_templates_are_capability_neutral():
    """Optional directive implementations must live in contributed resources."""
    templates = CORE_ANSIBLE / "roles/blitzecdn_nginx/templates"
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(templates.rglob("*.j2"))
    ).lower()
    forbidden = (
        "brotli ",
        "gzip_comp_level",
        "listen 443 quic",
        "alt-svc",
        "proxy_cache ",
        "proxy_cache_key",
        "blitzecdn_under_attack",
        "js_content",
        "$blitzecdn_country",
        "geoip2 ",
    )

    assert [directive for directive in forbidden if directive in text] == []


def test_the_decommission_play_names_no_capability_and_fills_its_slot():
    """The decommission play's capability slot.

    Decommissioning cannot be retried: once the host leaves inventory there is no
    way back, so a capability's files come off here or stay forever. The slot must
    be in the play, name no capability, and come before `blitzecdn_edge_teardown`,
    which ends by asserting the host is clean.
    """
    play = (CORE_ANSIBLE / "playbooks/decommission.yml").read_text(encoding="utf-8")
    contributed = {
        directory.name
        for roles in SOURCE.rglob("ansible/roles")
        if roles != CORE_ANSIBLE / "roles"
        for directory in roles.iterdir()
        if directory.is_dir()
    }

    for role in contributed:
        assert role not in play, (
            f"the decommission play names {role}, which is a capability's role"
        )
    assert "blitzecdn_edge_teardown_capability_roles" in play
    assert play.index("blitzecdn_edge_teardown_capability_roles") < play.index(
        "role: blitzecdn_edge_teardown"
    )


def test_core_s_teardown_removes_no_path_that_belongs_to_a_capability():
    """Core's teardown role names no capability's paths.

    It runs on every decommission, so it may remove only core's trees, the shared
    runtime directories and units matched by prefix. A capability's files come off
    in its own teardown role, in the slot before. The whole role is checked:
    defaults, tasks and handlers.
    """
    role = CORE_ANSIBLE / "roles/blitzecdn_edge_teardown"
    contributed_paths = ("resolved.conf.d", "sshd_config.d", "fail2ban", "sshd")

    for source in sorted(role.rglob("*.yml")):
        for line in source.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for path in contributed_paths:
                assert path not in line, (
                    f"{source.relative_to(CORE_ANSIBLE)} names {path}, which "
                    "belongs to a capability, not to core"
                )


def test_the_edge_play_names_no_capability_and_fills_both_slots():
    """The play is the platform's, and it is what makes a contribution run.

    It must name no capability's role and include both lists the control plane
    composes, or a contributed role would resolve by name and never execute.
    """
    play = (CORE_ANSIBLE / "playbooks/edge.yml").read_text(encoding="utf-8")
    contributed = {
        directory.name
        for roles in SOURCE.rglob("ansible/roles")
        if roles != CORE_ANSIBLE / "roles"
        for directory in roles.iterdir()
        if directory.is_dir()
    }

    for role in contributed:
        assert role not in play, (
            f"the edge play names {role}, which is a capability's role"
        )
    assert "blitzecdn_capability_roles" in play
    assert "blitzecdn_host_capability_roles" in play

    slot = (CORE_ANSIBLE / "roles/blitzecdn_capabilities/tasks/main.yml").read_text(
        encoding="utf-8"
    )
    assert "blitzecdn_capabilities_roles" in slot
    assert "include_role" in slot
    # One role, two invocations. Only `allow_duplicates` makes that deliberate;
    # without it, both slots reading the same variable would collapse into one.
    duplicates = (
        CORE_ANSIBLE / "roles/blitzecdn_capabilities/meta/main.yml"
    ).read_text(encoding="utf-8")
    assert "allow_duplicates: true" in duplicates
