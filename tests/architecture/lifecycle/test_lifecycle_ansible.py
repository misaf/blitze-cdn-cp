"""The deployment implementation attaches and detaches with the wheel."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

#: The `uv` this developer or this CI job is actually running, resolved once
#: rather than spelled as a bare name on every call. A partial path would be
#: whatever `PATH` happened to hold when a subprocess started, and these
#: subprocesses build and install wheels.
from lifecycle_support import (
    ANSIBLE_PACKAGE,
    PLATFORM_ROLES,
    _environment,
)
from paths import REPO_ROOT

ANSIBLE_ROLES = (
    "blitzecdn_cache_config",
    "blitzecdn_cache_purge",
    "blitzecdn_cache_stats",
)


#: The capability whose role core's *edge play* runs, which is the other half
#: of the Ansible contribution and a second independent capability role.
EDGE_ROLE_PACKAGE = "blitzecdn-geoip"


EDGE_ROLE = "blitzecdn_geoip"


#: And the capability that fills the play's *host* slot instead. A third
#: package in the same environment, because the property worth proving is that
#: the slots are composed independently: this one contributes no edge role and
#: no Nginx resource, and its roles must still arrive — in the host list, never
#: in the edge one.
#:
#: It pairs its host roles with a teardown role, so it reaches the decommission
#: slot too. That pairing is the point rather than an extra: the two files it
#: writes are at paths only this wheel knows, and core's `blitzecdn_edge_teardown`
#: used to name both — which is the leak the third slot exists to close.
HOST_ROLE_PACKAGE = "blitzecdn-hardening"


HOST_ROLES = ("blitzecdn_hardening_sshd", "blitzecdn_hardening_fail2ban")


HOST_TEARDOWN_ROLE = "blitzecdn_hardening_teardown"


#: And the capability that fills two slots at once, one of them in a different
#: play. `blitzecdn-resolver` converges in the *edge* slot and withdraws in the
#: decommission slot, which is the other shape the pairing takes: `hardening`
#: converges in the host slot and withdraws in the same decommission one. Two
#: packages reaching that slot from different halves of the edge play is what
#: makes it visible that core composes the list rather than either of them.
TEARDOWN_ROLE_PACKAGE = "blitzecdn-resolver"


TEARDOWN_EDGE_ROLE = "blitzecdn_resolver"


TEARDOWN_ROLE = "blitzecdn_resolver_teardown"


#: Both halves of the decommission slot, in the order core composes it: sorted
#: by plugin name, so `hardening` precedes `resolver`. Removal order is not the
#: reverse of convergence order and does not need to be — each role withdraws
#: only what its own package wrote.
TEARDOWN_ROLES = (HOST_TEARDOWN_ROLE, TEARDOWN_ROLE)


@pytest.fixture(scope="session")
def ansible_cycle(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
) -> dict[str, dict[str, object]]:
    """One environment, read at all three points of the cycle.

    Three subprocess reports rather than three virtualenvs: what is being
    claimed is that the *same* installation answers differently before, during
    and after, so building a separate environment per state would prove less
    at three times the cost.
    """
    environment = _environment(tmp_path_factory.mktemp("ansible-cycle") / "venv")
    environment.install(wheels["blitzecdn"])
    before = environment.ansible_roles()
    environment.install(
        wheels[ANSIBLE_PACKAGE],
        wheels[EDGE_ROLE_PACKAGE],
        wheels[HOST_ROLE_PACKAGE],
        wheels[TEARDOWN_ROLE_PACKAGE],
    )
    attached = environment.ansible_roles()
    environment.uninstall(
        ANSIBLE_PACKAGE,
        EDGE_ROLE_PACKAGE,
        HOST_ROLE_PACKAGE,
        TEARDOWN_ROLE_PACKAGE,
    )
    return {
        "before": before,
        "attached": attached,
        "after": environment.ansible_roles(),
    }


def test_core_alone_offers_no_capability_owned_role(
    ansible_cycle: dict[str, dict[str, object]],
):
    """The root wheel carries the platform's roles and nobody else's.

    The search path is core's directory and nothing beside it, because there is
    no contributed directory to add — and that directory is now inside the
    installed distribution, so the roles it holds are the platform's eleven
    rather than the empty list a fabricated path used to produce.
    """
    assert sorted(PLATFORM_ROLES) == ansible_cycle["before"]["roles"]
    assert len(ansible_cycle["before"]["paths"]) == 1
    assert "site-packages" in ansible_cycle["before"]["paths"][0]
    # And the edge play converges nothing beyond the platform. A core-only
    # installation renders `blitzecdn_capability_roles` as an empty list, which
    # is the shape the play's include loops over — not a missing variable it
    # would have to defend against.
    assert ansible_cycle["before"]["edge_roles"] == []
    assert ansible_cycle["before"]["host_roles"] == []
    assert ansible_cycle["before"]["teardown_roles"] == []
    # And it loads no dynamic module. Everything core renders — HTTP/1.1,
    # HTTP/2, HTTP/3, TLS, proxying, gzip — is compiled into Nginx, so a
    # capability-free edge has an empty `load_module` list rather than a
    # baseline one.
    assert ansible_cycle["before"]["modules"] == []
    assert not any(ansible_cycle["before"]["nginx"].values())


def test_installing_a_distribution_makes_its_roles_resolvable(
    ansible_cycle: dict[str, dict[str, object]],
):
    """Attach, on the Ansible side. One `pip install` and the roles are there.

    And they are there *inside the installed distribution* — the asserted path
    is under the virtualenv, so this cannot be passing because the repository
    checkout happens to be on disk beside it.
    """
    attached = ansible_cycle["attached"]

    assert (
        sorted(
            [
                # The platform's, which are there in every state of the cycle
                # because they ship in the root wheel.
                *PLATFORM_ROLES,
                *ANSIBLE_ROLES,
                EDGE_ROLE,
                *HOST_ROLES,
                TEARDOWN_EDGE_ROLE,
                *TEARDOWN_ROLES,
            ]
        )
        == attached["roles"]
    )
    contributed = attached["paths"][1]
    assert "site-packages" in contributed
    assert "blitzecdn_cache/ansible/roles" in contributed
    assert str(REPO_ROOT) not in contributed
    nginx = attached["nginx"]
    assert {resource["plugin"] for values in nginx.values() for resource in values} == {
        "cache",
        "geoip",
    }
    assert all(resource["exists"] for values in nginx.values() for resource in values)


def test_installing_a_distribution_puts_its_role_into_the_edge_play(
    ansible_cycle: dict[str, dict[str, object]],
):
    """Attach, all the way to what a deploy actually converges.

    Resolving the role by name is not enough on its own: a role nothing
    includes changes no edge. The contribution carries both halves, so
    installing the wheel is what puts `blitzecdn_geoip` in the list core's edge
    play loops over — and `blitzecdn-cache`, installed in the same environment,
    contributes roles its own plays reach and nothing to the play, which is
    what makes the two halves visibly separate.
    """
    assert ansible_cycle["attached"]["edge_roles"] == [
        "blitzecdn_cache_config",
        EDGE_ROLE,
        TEARDOWN_EDGE_ROLE,
    ]


def test_installing_a_distribution_puts_its_module_into_the_edge_s_load_list(
    ansible_cycle: dict[str, dict[str, object]],
):
    """Attach, all the way to what the running Nginx loads.

    The last place a capability used to survive its own removal. `geoip2` was
    built into the edge image and loaded by a file inside the image, so an
    installation with no `blitzecdn-geoip` still loaded the module on every
    edge — the image is built once and pinned by digest, and nothing about
    detaching a distribution could reach it.

    Declaring the module with the capability makes the `load_module` list a
    function of what is installed: `blitzecdn-cache`, `blitzecdn-hardening` and
    `blitzecdn-resolver` are in this same environment and need none, and the
    module named here is named by exactly one wheel.
    """
    assert ansible_cycle["attached"]["modules"] == [["geoip", "geoip2"]]


def test_installing_a_host_capability_fills_the_other_slot_only(
    ansible_cycle: dict[str, dict[str, object]],
):
    """The two slots are composed independently, from one set of contributions.

    `blitzecdn-hardening` declares `host_roles` and `teardown_roles` and
    nothing else: no edge role, no Nginx resource, no environment key, no
    desired-state variable. So its converging roles must appear in the list the
    play's host slot loops over and in neither of the others — a package
    landing in the edge slot instead would run SSH hardening before the
    firewall was validated, which is how a host ends up key-only and
    unreachable at once. Its withdrawal is a third role and is asserted
    separately, below.

    Declared order inside the contribution is kept, because that package alone
    owns both roles and Fail2Ban's jail has to protect a daemon that has
    already stopped accepting passwords.
    """
    attached = ansible_cycle["attached"]

    assert attached["host_roles"] == list(HOST_ROLES)
    assert not set(HOST_ROLES) & set(attached["edge_roles"])
    assert "hardening" not in {
        resource["plugin"]
        for values in attached["nginx"].values()
        for resource in values
    }


def test_a_capability_that_writes_outside_core_s_trees_can_take_it_off_again(
    ansible_cycle: dict[str, dict[str, object]],
):
    """The decommission slot, and the pairing it exists for.

    `blitzecdn-resolver` writes a drop-in under /etc/systemd/resolved.conf.d;
    `blitzecdn-hardening` writes an SSH policy under /etc/ssh/sshd_config.d and
    a jail under /etc/fail2ban/jail.d. Core's `blitzecdn_edge_teardown` removes the
    trees it wrote, the shared runtime directories and every systemd unit
    matching the managed prefix — none of those three files is any of those,
    and core naming one would put a path belonging to a wheel into a role that
    is installed whether or not the wheel is. It did name two of them, which is
    what this pair of packages between them now takes out of core.

    So the removal travels with the capability, in a slot of its own, and both
    packages reach that slot from a different half of the edge play: resolver
    converges in the edge slot, hardening in the host slot. Whichever half a
    capability converges from, its withdrawal lands here — and never in either
    of the other two, since a teardown role in the edge slot would strip
    resolution, or host access, from every edge on every deploy.
    """
    attached = ansible_cycle["attached"]

    # Sorted by plugin name, like every slot: two independent packages, one
    # list, composed by core rather than by either of them.
    assert attached["teardown_roles"] == list(TEARDOWN_ROLES)
    for role in TEARDOWN_ROLES:
        assert role not in attached["edge_roles"]
        assert role not in attached["host_roles"]
    # And neither package's *converging* roles leak the other way. Withdrawing
    # is a separate role in both cases, which is what keeps a decommission from
    # re-converging the very policy it is removing.
    assert TEARDOWN_EDGE_ROLE not in attached["teardown_roles"]
    assert not set(HOST_ROLES) & set(attached["teardown_roles"])
    # `blitzecdn-cache` and `blitzecdn-geoip` are installed in this same
    # environment and reach this slot with nothing: a capability that writes
    # only inside the trees core already removes declares no teardown role, and
    # absence here is the correct answer rather than an omission.
    assert not {*ANSIBLE_ROLES, EDGE_ROLE} & set(attached["teardown_roles"])


def test_uninstalling_a_distribution_takes_its_roles_with_it(
    ansible_cycle: dict[str, dict[str, object]],
):
    """Detach, on the Ansible side, and the acceptance criterion in full.

    The Python capability and its deployment implementation leave together.
    Nothing in core is edited, no directory is pruned by hand, and the role
    names simply stop resolving on the next run. The edge play stops running
    the capability's role in the same breath, which is the part that means an
    already-converged edge is left alone rather than half-managed.
    """
    assert ansible_cycle["after"] == ansible_cycle["before"]
    assert ansible_cycle["after"]["edge_roles"] == []
    assert ansible_cycle["after"]["host_roles"] == []
    # Including the removal role. A capability that leaves takes its teardown
    # with it, which is why core's own teardown may not depend on one having
    # ever been installed.
    assert ansible_cycle["after"]["teardown_roles"] == []
    # And the module goes with it. This is the half the edge image used to get
    # wrong on its own: the role and the Nginx resources disappeared while the
    # module kept loading, because the image had been built naming it.
    assert ansible_cycle["after"]["modules"] == []
    assert not any(ansible_cycle["after"]["nginx"].values())


def test_the_capability_wheel_carries_its_whole_ansible_tree(
    wheels: dict[str, Path],
):
    """Built, not assumed. An editable install would hide the real failure.

    A wheel that shipped only the `.py` files would leave a plugin pointing at
    a directory that does not exist on an installed controller, and every
    purge would fail with "the role was not found" — on the controller, never
    in this checkout.
    """
    import zipfile

    with zipfile.ZipFile(wheels[ANSIBLE_PACKAGE]) as archive:
        names = set(archive.namelist())

    root = "blitzecdn_cache/ansible"
    for role in ANSIBLE_ROLES:
        assert f"{root}/roles/{role}/tasks/main.yml" in names
        assert f"{root}/roles/{role}/defaults/main.yml" in names
        assert f"{root}/roles/{role}/meta/argument_specs.yml" in names
    # Not only YAML: the statistics role reads the access log with a shipped
    # script, and a build that filtered by extension would drop it silently.
    assert f"{root}/roles/blitzecdn_cache_stats/files/collect-cache-stats.sh" in names
    assert f"{root}/playbooks/cache-purge.yml" in names
    assert f"{root}/playbooks/stats.yml" in names


def test_the_root_wheel_carries_no_capability_owned_ansible(
    wheels: dict[str, Path],
):
    """The other direction: core's tree kept nothing behind when they moved."""
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn"]) as archive:
        names = "".join(archive.namelist())

    assert "blitzecdn_cache" not in names
    assert "blitzecdn_cache_stats" not in names
    assert "cache-purge.yml" not in names
    assert "acme-challenge.yml" not in names


@pytest.mark.parametrize(
    ("distribution", "module", "resources"),
    [
        (
            "blitzecdn-cache",
            "blitzecdn_cache",
            {"cache-http.conf.j2", "cache-upstream.conf.j2"},
        ),
        (
            "blitzecdn-compression",
            "blitzecdn_compression",
            {"compression-server.conf.j2"},
        ),
        (
            "blitzecdn-http3",
            "blitzecdn_http3",
            {"http3-server.conf.j2", "http3-upstream.conf.j2"},
        ),
        (
            "blitzecdn-geoip",
            "blitzecdn_geoip",
            {"geoip-http.conf.j2", "geoip-upstream.conf.j2"},
        ),
        (
            "blitzecdn-security",
            "blitzecdn_security",
            {
                "security-http.conf.j2",
                "security-server.conf.j2",
                "security-access.conf.j2",
                "security-upstream.conf.j2",
            },
        ),
    ],
)
def test_capability_wheels_carry_their_nginx_resources(
    wheels: dict[str, Path],
    distribution: str,
    module: str,
    resources: set[str],
):
    import zipfile

    with zipfile.ZipFile(wheels[distribution]) as archive:
        names = set(archive.namelist())

    assert {f"{module}/nginx/{resource}" for resource in resources} <= names


def test_an_installed_capability_locates_its_plays_without_the_repository(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
):
    """Resource discovery, asked of a real install from outside the checkout.

    The subprocess runs with its working directory somewhere else entirely, so
    a path built from `cwd` or from a repository-relative walk would fail here
    and only here.
    """
    environment = _environment(tmp_path_factory.mktemp("resources") / "venv")
    environment.install(wheels["blitzecdn"], wheels[ANSIBLE_PACKAGE])
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    program = (
        "import json;"
        "from blitzecdn_cache import ansible;"
        "print(json.dumps({"
        "'purge': str(ansible.CACHE_PURGE_PLAYBOOK),"
        "'exists': ansible.CACHE_PURGE_PLAYBOOK.is_file()"
        " and ansible.STATS_PLAYBOOK.is_file()"
        " and (ansible.ROLES_PATH / 'blitzecdn_cache_purge').is_dir(),"
        "}))"
    )
    finished = subprocess.run(
        [str(environment.python), "-c", program],
        capture_output=True,
        text=True,
        check=True,
        cwd=elsewhere,
        timeout=300,
    )
    resolved = json.loads(finished.stdout)

    assert resolved["exists"]
    assert str(environment.root) in resolved["purge"]
