"""Package ansible boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from package_boundary_support import (
    _entry_point_names,
    _import_package,
    _packages,
)
from paths import REPO_ROOT, SOURCE, optional_packages

from blitzecdn.composition import load_control_plane_plugins

#: The variables that are the interface *between* roles rather than one role's
#: own input, so they are the exception to the prefix rule below.
#:
#: `blitzecdn_edge_runtime` is the resolved edge contract — where nginx keeps
#: its state, which ports it listens on, which image it runs — and eight roles
#: read it. `blitzecdn_nginx_sites` is the site document four roles project
#: from, and `blitzecdn_nginx_http3_listener_owner` is how `http3` tells
#: `nginx` which of them opened the QUIC listener.
#:
#: Written down because they had no home. Each is declared in whichever roles
#: happen to consume it, and until this list existed the difference between "a
#: shared fleet fact" and "a role reaching for a name it does not own" was
#: nowhere — which is exactly how the second one arrives.
_SHARED_FLEET_VARIABLES = frozenset(
    {
        "blitzecdn_edge_runtime",
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


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_a_role_is_named_for_the_distribution_that_ships_it(package: Path):
    """An operator reading a role name should know which wheel to install.

    Ansible resolves roles by name off a flat search path, so a role name is
    global: it collides with every other role on that path, and it is what
    appears in an operator's own playbooks and in `--tags`. Nothing constrained
    them, and three had drifted — `blitzecdn_stats` shipped in
    `blitzecdn-cache`, `blitzecdn_sshd` and `blitzecdn_fail2ban` in
    `blitzecdn-hardening`.

    `stats` was the clearest: the *Python* `stats` command had already been
    moved out of `diagnostics` for this exact reason — "it reads the cache
    capability's report, so it belongs to the distribution that produces one" —
    and the role it drives kept the old name. The other two are named for the
    tool they configure, which is the shape `_STRATEGIES_OWNED_BY_A_CAPABILITY`
    refuses for a Python package: `fail2ban` is to `hardening` what `gzip` is to
    `compression`, an implementation choice rather than a capability.

    Core is exempt from the second half. Its roles serve every capability —
    `blitzecdn_nginx` renders whatever the merged document holds — so there is
    no one capability to name them after.
    """
    root = package / "src" / _import_package(package) / "ansible" / "roles"
    if not root.is_dir():
        pytest.skip(f"{package.name} ships no roles")
    expected = _import_package(package)
    offenders = [
        f"{role} ships in {package.name} and should be named {expected}[_*]"
        for role, _ in _role_specifications(root)
        if role != expected and not role.startswith(f"{expected}_")
    ]
    assert offenders == []


def test_a_roles_variables_carry_its_name():
    """Ansible's own convention, and the reason it exists.

    Variables are global to a play. A role declaring `ssh_port` would collide
    with every other role that wanted one, and the prefix is what keeps eight
    roles' inputs from being one namespace. Every role in the workspace already
    followed it; nothing said so, and renaming a role is exactly the moment the
    variables stop matching — `blitzecdn_stats_status_port` sitting in a role
    now called `blitzecdn_cache_stats` reads as somebody else's variable.

    The exceptions are declared above and are genuinely shared. Growing that
    list should be a decision: it is the interface between roles, and every
    name on it is one more thing two roles have to agree about.
    """
    roots = [(SOURCE / "ansible" / "roles", "blitzecdn")]
    roots += [
        (package / "src" / _import_package(package) / "ansible" / "roles", "")
        for package in _packages()
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
# The other half of a vertical slice, and the half that used to be missing.
# A capability whose Python detaches cleanly while its roles, its templates and
# its fleet settings stay behind in the control plane's `ansible/` tree is not
# detachable at all: uninstalling the wheel leaves an edge still being told to
# provision a GeoIP database by a role core carries.
#
# Three rules hold it, and each one refuses a different way of half-doing the
# move: no capability's name in core's Ansible, no capability's role in core's
# tree, and a contributed role that the edge play really runs.
# ----------------------------------------------------------------------

#: The repository-level Ansible tree, which is the platform's and nobody
#: else's: the base host, the container engine, the firewall, the edge runtime
#: contract, `blitzecdn_nginx`, and the slot the installed capabilities fill.
CORE_ANSIBLE = REPO_ROOT / "src/blitzecdn/ansible"


#: Words that name an optional capability's *implementation* rather than a
#: setting core legitimately renders. `geoip2` is the Nginx module directive,
#: `geoipupdate` the updater container, `maxmind` the account they authenticate
#: to, `njs`/`js_import` the module system the challenge is written in — none
#: of them can appear in a tree that must converge an edge identically whether
#: or not any distribution is installed.
#:
#: `resolved.conf.d` is the resolver drop-in's directory: the file there is
#: written by `blitzecdn-resolver`'s role and removed by that same package's
#: teardown role, so core neither creates nor deletes it. It used to be listed
#: in `blitzecdn_edge_teardown`'s defaults, which meant a role installed on every
#: controller carried the path of a capability that may not be installed.
#: `sshd_config.d` and `fail2ban` are the same case one package later: both
#: files are `blitzecdn-hardening`'s, and `blitzecdn_hardening_teardown` is
#: what removes them.
#:
#: `$blitzecdn_country` and `under_attack_mode` are deliberately *not* here.
#: They are site settings core renders from desired state, and a site that asks
#: for either is refused by name before a play starts; that split — core reads
#: the variable, the capability defines it — is the whole design and is
#: asserted from the packages' own tests, which can see both sides.
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


#: There are no exemptions, and there is no longer a mechanism for one.
#:
#: There used to be exactly one: the task that started the managed edge image
#: and asked whether it could load the modules it was built with named GeoIP2,
#: Brotli and njs, on the grounds that the *image* carries them whether or not
#: any distribution is installed. That grounding is what changed. The modules
#: an edge loads are declared by the capabilities that need them, resolved by
#: `blitzecdn.core.plugins.resolution.resolve_edge_modules` and rendered into
#: the edge's own `load_module` list — so the probe now asserts against that
#: resolved list and has no module name of its own to hold. Nothing in the tree
#: is exempt.
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

    A grep, deliberately, because the failure it catches is textual: a task
    that fetches a MaxMind database or a template that emits `js_import` is
    implementation belonging to a wheel, wherever it is written and whatever
    the variable around it is called.

    A comment is exempt. Several of the files here explain *why* something is
    no longer present, and forbidding the explanation would mean removing the
    only record of the decision.
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
    """The same rule as a directory listing, which is what a duplicate is.

    A role left behind in `ansible/roles/` after its package shipped one would
    not merely be dead: `resolve_role_search_path` puts core's directory first,
    so the stale copy would *win* and every edge would converge the version
    nobody is maintaining.
    """
    contributed = {
        directory.name
        for package in optional_packages()
        for directory in (package / "src").rglob("ansible/roles/*")
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
    """The other play a capability contributes to, and the harder direction.

    Converging is forgiving: a capability whose role did not run leaves an edge
    unconverged, and the next deploy fixes it. Decommissioning is not. The play
    runs while the host is still in inventory and there is no way back to it
    afterwards, so a capability's files either come off here or stay on that
    host forever.

    The slot must therefore be *in* the play, must name no capability, and must
    come before `blitzecdn_edge_teardown` — that role ends by asserting the host is
    clean and failing the run if anything survived, which is the verdict on the
    whole decommission and cannot be passed before half the removal has
    happened.
    """
    play = (CORE_ANSIBLE / "playbooks/decommission.yml").read_text(encoding="utf-8")
    contributed = {
        directory.name
        for package in optional_packages()
        for directory in (package / "src").rglob("ansible/roles/*")
        if directory.is_dir()
    }

    for role in contributed:
        assert role not in play, (
            f"the decommission play names {role}, which ships in a wheel"
        )
    assert "blitzecdn_edge_teardown_capability_roles" in play
    assert play.index("blitzecdn_edge_teardown_capability_roles") < play.index(
        "role: blitzecdn_edge_teardown"
    )


def test_core_s_teardown_removes_no_path_that_belongs_to_a_wheel():
    """A role installed on every controller may not know a wheel's paths.

    `blitzecdn_edge_teardown` is core's, so it runs on every decommission whatever
    is installed. Every path in it is therefore a claim core can still make
    when a capability has been detached — its own trees, the shared runtime
    directories, and units matched by prefix rather than listed. A capability's
    own file is removed by that capability's teardown role, in the slot before
    this one.

    The whole role rather than only its defaults, because the leak this refuses
    has appeared in all three files: the SSH policy and the Fail2Ban jail were
    paths in `defaults`, a task removing them, *and* two handlers reloading
    services only `blitzecdn-hardening` installs. A defaults-only check would
    have passed while core still restarted `fail2ban` on every decommission.
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
                    "belongs to a wheel core cannot depend on being installed"
                )


def test_the_edge_play_names_no_capability_and_fills_both_slots():
    """The play is the platform's, and it is what makes a contribution run.

    Two properties in one place because they are the same decision. The play
    must name no capability's role — that is what "no edit to add a package"
    means — and it must include both lists the control plane composes, or a
    contributed role would resolve by name and never execute.

    Both slots, because a package declaring `host_roles` and getting silence is
    the failure this catches: the edge slot would still run, the play would
    still report success, and the SSH policy nobody noticed was missing is
    exactly the kind of absence that surfaces months later.
    """
    play = (CORE_ANSIBLE / "playbooks/edge.yml").read_text(encoding="utf-8")
    contributed = {
        directory.name
        for package in optional_packages()
        for directory in (package / "src").rglob("ansible/roles/*")
        if directory.is_dir()
    }

    for role in contributed:
        assert role not in play, f"the edge play names {role}, which ships in a wheel"
    assert "blitzecdn_capability_roles" in play
    assert "blitzecdn_host_capability_roles" in play

    slot = (CORE_ANSIBLE / "roles/blitzecdn_capabilities/tasks/main.yml").read_text(
        encoding="utf-8"
    )
    assert "blitzecdn_capabilities_roles" in slot
    assert "include_role" in slot
    # One role, two invocations. Ansible runs a role again when its parameters
    # differ, but only `allow_duplicates` states that on purpose — without it,
    # an edit that made both slots read the same variable would silently
    # collapse them into one and converge the host slot's roles nowhere.
    duplicates = (
        CORE_ANSIBLE / "roles/blitzecdn_capabilities/meta/main.yml"
    ).read_text(encoding="utf-8")
    assert "allow_duplicates: true" in duplicates


def test_every_contributed_edge_role_is_shipped_by_the_plugin_that_asks_for_it():
    """A contribution that names a role its own wheel lacks is refused early.

    Ansible would refuse it too, but only after the engine is installed, the
    image pulled and the play half-way through an edge — and its message names
    the role, never the distribution that asked for it.
    """
    for package in optional_packages():
        for roles in (package / "src").rglob("ansible/roles"):
            available = {path.name for path in roles.iterdir() if path.is_dir()}
            plugin = (roles.parent.parent / "plugin.py").read_text(encoding="utf-8")
            for role in available:
                if role in plugin:
                    assert (roles / role / "tasks/main.yml").is_file(), role


def test_optional_roles_do_not_depend_on_another_packages_role_order():
    """Alphabetical contribution order is deterministic, never dependency resolution."""
    package_roles = {
        package: {
            role.name
            for roles in (package / "src").rglob("ansible/roles")
            for role in roles.iterdir()
            if role.is_dir()
        }
        for package in optional_packages()
    }
    offenders = []
    for package, own_roles in package_roles.items():
        foreign_roles = set().union(
            *(roles for owner, roles in package_roles.items() if owner != package)
        )
        for task_file in (package / "src").rglob("ansible/roles/*/tasks/*.yml"):
            text = task_file.read_text(encoding="utf-8")
            offenders.extend(
                f"{task_file.relative_to(REPO_ROOT)} names {role}"
                for role in sorted(foreign_roles - own_roles)
                if f"role: {role}" in text or f"name: {role}" in text
            )

    assert offenders == []


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_the_role_named_for_the_wheel_is_the_one_that_converges_an_edge(
    package: Path,
):
    """The bare name belongs to the role that does the wheel's ordinary work.

    `blitzecdn-cache` shipped three roles, and the unsuffixed one — the name an
    operator reads as *the* cache role — was the purge. Purging runs in its own
    play, on demand, converging nothing; the role that actually gives an edge a
    cache was `blitzecdn_cache_config`. So the shortest name pointed at the
    least representative role, and the one a `--tags` line or a hand-written
    play would reach for first did the wrong thing.

    `blitzecdn-resolver` has it the right way round: `blitzecdn_resolver`
    converges and `blitzecdn_resolver_teardown` undoes it. The rule is that
    arrangement stated — if a wheel names a role after itself, that role has to
    be one it contributes to a slot core actually runs, rather than a play the
    wheel invokes for an operation.

    A wheel that contributes nothing to a core play is exempt, because nothing
    competes for the name: `blitzecdn-origins` ships one role and reaching the
    origins it proxies to is all that wheel does, so `blitzecdn_origins` is
    exactly right. The rule bites only where a wheel has both kinds of role and
    gave the shorter name to the wrong one.

    Derived from the plugin's own contribution, so a wheel that later moves its
    converging work into a suffixed role fails here rather than leaving the
    bare name pointing at whatever is left.
    """
    root = package / "src" / _import_package(package) / "ansible" / "roles"
    if not root.is_dir():
        pytest.skip(f"{package.name} ships no roles")
    bare = _import_package(package)
    if bare not in {role for role, _ in _role_specifications(root)}:
        pytest.skip(f"{package.name} ships no role named {bare}")

    claims = _entry_point_names(package)
    contributed = {
        role
        for contribution in load_control_plane_plugins().ansible_contributions()
        if contribution.plugin in claims
        for slot in ("edge_roles", "host_roles", "teardown_roles")
        for role in getattr(contribution, slot)
    }
    if not contributed:
        pytest.skip(
            f"{package.name} contributes no role to a core play; nothing "
            f"competes with {bare} for the name"
        )
    assert bare in contributed, (
        f"{bare} is the name an operator reads as the {package.name} role, but "
        f"the roles {package.name} contributes to a play core runs are "
        f"{sorted(contributed)}. Name the operation for what it does, and "
        "leave the bare name to the role that converges an edge."
    )
