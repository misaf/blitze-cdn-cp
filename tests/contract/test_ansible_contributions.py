"""What an installed capability contributes to Ansible, and how core composes it.

The role search path is the larger half: an optional capability is only whole
if its deployment implementation travels with it, and Ansible resolves a role
name against a single process-wide list. So the composition of that list is a
contract in its own right: what is in it, in which order, and what happens when
two packages claim one name. The dynamic modules an edge loads are the same
shape of question one level down — `load_module` is a main-context directive,
so there is one list per Nginx process too.

An optional capability is only whole if its deployment implementation travels
with it, and Ansible resolves a role name against a single process-wide list.
So the composition of that list is a contract in its own right: what is in it,
in which order, and what happens when two packages claim one name.

Nothing here installs anything. The property being asserted is the *rule*, so
the contributions are built by hand; `tests/architecture/test_lifecycle.py`
asserts the other half — that a real wheel really carries the directory a real
plugin really contributes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from control_plane_fixtures import FakeEdgeStore, settings  # noqa: F401

from blitzecdn.capabilities.edges.adapters.roster import EdgeRoster
from blitzecdn.core import ansible
from blitzecdn.core.ansible import execution as ansible_execution
from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.plugins import AnsibleContribution, EdgeModule
from blitzecdn.core.plugins.resolution import (
    resolve_edge_capability_roles,
    resolve_edge_modules,
    resolve_host_capability_roles,
    resolve_role_search_path,
    resolve_teardown_capability_roles,
)


def _roles(root: Path, *names: str) -> Path:
    for name in names:
        (root / name / "tasks").mkdir(parents=True)
        (root / name / "tasks/main.yml").write_text("---\n[]\n", encoding="utf-8")
    return root


def test_core_roles_come_first_and_contributed_roles_follow(tmp_path):
    core = _roles(tmp_path / "core", "blitzecdn_nginx")
    cache = _roles(tmp_path / "cache", "blitzecdn_cache")

    search = resolve_role_search_path(
        core, [AnsibleContribution(plugin="cache", roles_path=cache)]
    )

    assert search == (core, cache)


def test_the_order_follows_the_plugins_not_the_order_they_registered(tmp_path):
    """Two installations with the same packages resolve every role alike.

    Pluggy calls implementations last-registered-first, and registration order
    depends on entry-point iteration. A search path that inherited it would
    make "which directory answers for this role" a property of the environment
    rather than of what is installed.
    """
    core = _roles(tmp_path / "core")
    waf = _roles(tmp_path / "waf", "blitzecdn_waf")
    cache = _roles(tmp_path / "cache", "blitzecdn_cache")
    contributions = [
        AnsibleContribution(plugin="waf", roles_path=waf),
        AnsibleContribution(plugin="cache", roles_path=cache),
    ]

    assert resolve_role_search_path(core, contributions) == (core, cache, waf)
    assert resolve_role_search_path(core, reversed(contributions)) == (
        core,
        cache,
        waf,
    )


def test_an_absent_package_contributes_nothing(tmp_path):
    """Detachment is not a special case: there is simply nothing to add."""
    core = _roles(tmp_path / "core", "blitzecdn_nginx")

    assert resolve_role_search_path(core, []) == (core,)


def test_a_role_two_packages_both_ship_is_refused_by_name(tmp_path):
    core = _roles(tmp_path / "core")
    first = _roles(tmp_path / "first", "blitzecdn_purge")
    second = _roles(tmp_path / "second", "blitzecdn_purge")

    with pytest.raises(PluginError) as error:
        resolve_role_search_path(
            core,
            [
                AnsibleContribution(plugin="alpha", roles_path=first),
                AnsibleContribution(plugin="zulu", roles_path=second),
            ],
        )

    assert "blitzecdn_purge" in str(error.value)
    assert "'alpha'" in str(error.value)
    assert "'zulu'" in str(error.value)


def test_a_package_may_not_shadow_a_role_the_control_plane_owns(tmp_path):
    """The dangerous direction, and the reason this is not merely a warning.

    Ansible takes the first directory that has the name. A package shipping
    `blitzecdn_nginx` would replace the edge's configuration renderer, and the
    deployment would succeed while converging something nobody wrote.
    """
    core = _roles(tmp_path / "core", "blitzecdn_nginx")
    impostor = _roles(tmp_path / "impostor", "blitzecdn_nginx")

    with pytest.raises(PluginError) as error:
        resolve_role_search_path(
            core, [AnsibleContribution(plugin="impostor", roles_path=impostor)]
        )

    assert "the control plane" in str(error.value)
    assert "'impostor'" in str(error.value)


def test_a_contributed_directory_that_is_not_there_is_refused(tmp_path):
    """A wheel built without its Ansible tree fails here, naming the package.

    Left to Ansible it would surface much later as "the role was not found",
    which names neither the distribution that promised it nor the fact that its
    files never shipped.
    """
    core = _roles(tmp_path / "core")

    with pytest.raises(PluginError) as error:
        resolve_role_search_path(
            core,
            [AnsibleContribution(plugin="cache", roles_path=tmp_path / "nowhere")],
        )

    assert "'cache'" in str(error.value)
    assert "does not exist" in str(error.value)


def test_the_resolved_path_is_what_ansible_is_actually_given(
    settings,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """The composition reaches the subprocess, not just the constructor.

    `ANSIBLE_ROLES_PATH` rather than `ansible.cfg`, because the cfg can only
    name a directory relative to itself and an installed wheel is nowhere near
    it.
    """
    cache = _roles(tmp_path / "cache", "blitzecdn_cache")
    captured: dict[str, str] = {}

    def fake_run(**kwargs):
        captured.update(kwargs["envvars"])
        raise AssertionError("stop after the environment is built")

    monkeypatch.setattr(ansible_execution.ansible_runner, "run", fake_run)
    runner = ansible.AnsibleRunner(
        settings,
        EdgeRoster(FakeEdgeStore()),
        resolve_role_search_path(
            settings.ansible_dir / "roles",
            [AnsibleContribution(plugin="cache", roles_path=cache)],
        ),
    )
    with pytest.raises(AssertionError):
        runner.run(check=True)

    assert captured["ANSIBLE_ROLES_PATH"].split(":") == [
        str(settings.ansible_dir / "roles"),
        str(cache),
    ]


def test_a_runner_nobody_gave_a_path_still_finds_the_control_plane_s_roles(
    settings,  # noqa: F811
    monkeypatch,
):
    """Core's own plays must not need a plugin registry to run."""
    captured: dict[str, str] = {}

    def fake_run(**kwargs):
        captured.update(kwargs["envvars"])
        raise AssertionError("stop after the environment is built")

    monkeypatch.setattr(ansible_execution.ansible_runner, "run", fake_run)
    with pytest.raises(AssertionError):
        ansible.AnsibleRunner(settings, EdgeRoster(FakeEdgeStore())).run(check=False)

    assert captured["ANSIBLE_ROLES_PATH"] == str(settings.ansible_dir / "roles")


# --- the capability slots ---------------------------------------------------


def test_a_slot_refuses_a_role_the_package_does_not_ship(tmp_path):
    """Refused at composition, naming the distribution that asked.

    Ansible would refuse it too, but only once the play had reached the include
    — with the engine installed, the image pulled and, in the decommission
    slot, a host already half taken apart. And it would name the role rather
    than the wheel, which is the part that leads nobody anywhere.
    """
    roles = _roles(tmp_path / "resolver", "blitzecdn_resolver")
    contribution = AnsibleContribution(
        plugin="resolver",
        roles_path=roles,
        teardown_roles=("blitzecdn_resolver_teardown",),
    )

    with pytest.raises(PluginError) as error:
        resolve_teardown_capability_roles([contribution])

    assert "'resolver'" in str(error.value)
    assert "blitzecdn_resolver_teardown" in str(error.value)
    assert "teardown_roles" in str(error.value)


def test_the_three_slots_are_composed_independently(tmp_path):
    """One set of contributions, three lists, no leakage between them.

    A role landing in the wrong slot is not a cosmetic error: the edge slot
    runs before the firewall is validated and the teardown slot runs on a host
    that is leaving inventory. Each list must hold exactly what was declared
    for it.
    """
    roles = _roles(
        tmp_path / "pkg", "converge_role", "host_role", "withdraw_role", "own_play_role"
    )
    contributions = [
        AnsibleContribution(
            plugin="one",
            roles_path=roles,
            edge_roles=("converge_role",),
            host_roles=("host_role",),
            teardown_roles=("withdraw_role",),
        )
    ]

    assert resolve_edge_capability_roles(contributions) == ("converge_role",)
    assert resolve_host_capability_roles(contributions) == ("host_role",)
    assert resolve_teardown_capability_roles(contributions) == ("withdraw_role",)


def test_a_package_declaring_no_slot_contributes_to_none(tmp_path):
    """Shipping a role its own plays reach is not a contribution to a slot."""
    roles = _roles(tmp_path / "cache", "blitzecdn_stats")
    contributions = [AnsibleContribution(plugin="cache", roles_path=roles)]

    assert resolve_edge_capability_roles(contributions) == ()
    assert resolve_host_capability_roles(contributions) == ()
    assert resolve_teardown_capability_roles(contributions) == ()


def test_every_slot_reaches_ansible_on_the_command_line(
    settings,  # noqa: F811
    monkeypatch,
):
    """The composed lists reach the subprocess, not just the constructor.

    Each slot's role includes the list by name, so a slot the executor did not
    forward is a slot that silently converges nothing — the play still reports
    success, and the capability simply never ran. That is the failure this
    catches, and it is invisible in every other test: the play parses, the
    roles resolve, and nothing happens.

    On the command line rather than in the variables file, because that file
    *is* the desired-state snapshot a rollback converges months later, and what
    is installed today is not desired state.
    """
    captured: dict[str, str] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        raise AssertionError("stop once the command line is built")

    monkeypatch.setattr(ansible_execution.ansible_runner, "run", fake_run)
    runner = ansible.AnsibleRunner(
        settings,
        EdgeRoster(FakeEdgeStore()),
        capability_roles=("converge_role",),
        host_capability_roles=("host_role",),
        teardown_capability_roles=("withdraw_role",),
        edge_modules=resolve_edge_modules(
            [
                AnsibleContribution(
                    plugin="one",
                    roles_path=settings.ansible_dir / "roles",
                    edge_modules=(EdgeModule(name="brotli", objects=("filter.so",)),),
                )
            ]
        ),
    )
    with pytest.raises(AssertionError):
        runner.run(check=True)

    command = captured["cmdline"]
    assert '"blitzecdn_capability_roles": ["converge_role"]' in command
    assert '"blitzecdn_host_capability_roles": ["host_role"]' in command
    assert '"blitzecdn_teardown_capability_roles": ["withdraw_role"]' in command
    # The same journey for the module list, and for the same reason: an edge
    # loads what the extra-var says and nothing else, so a list the executor
    # did not forward is an edge whose contributed directives are unknown.
    assert '"blitzecdn_nginx_modules"' in command
    assert '"objects": ["filter.so"]' in command


# --- the dynamic modules an installed capability loads -----------------------


def test_the_module_list_is_ordered_by_plugin_and_carries_who_asked():
    """`load_module` is one list per process, so core composes it.

    Ordered by plugin name like every other slot, so two controllers with the
    same packages installed render the same file and an edge's configuration
    tree does not change because pluggy registered in a different order.
    """
    contributions = [
        AnsibleContribution(
            plugin="security",
            roles_path=Path("/nowhere"),
            edge_modules=(EdgeModule(name="njs", objects=("js.so",), build=False),),
        ),
        AnsibleContribution(
            plugin="compression",
            roles_path=Path("/nowhere"),
            edge_modules=(EdgeModule(name="brotli", objects=("brotli.so",)),),
        ),
    ]

    resolved = resolve_edge_modules(contributions)

    assert [(module.plugin, module.name) for module in resolved] == [
        ("compression", "brotli"),
        ("security", "njs"),
    ]
    assert [module.build for module in resolved] == [True, False]


def test_two_capabilities_may_need_the_same_module():
    """njs is one shared object, and any number of capabilities may want it.

    Refusing the second would make one capability declare a dependency on the
    other over a file neither of them owns.
    """
    njs = EdgeModule(name="njs", objects=("js.so",), build=False, probe="js_path /e;")
    contributions = [
        AnsibleContribution(
            plugin="alpha", roles_path=Path("/nowhere"), edge_modules=(njs,)
        ),
        AnsibleContribution(
            plugin="zulu", roles_path=Path("/nowhere"), edge_modules=(njs,)
        ),
    ]

    assert [module.name for module in resolve_edge_modules(contributions)] == ["njs"]


def test_one_module_described_two_ways_is_refused():
    """Two descriptions of one module means an edge loads whichever won."""
    contributions = [
        AnsibleContribution(
            plugin="alpha",
            roles_path=Path("/nowhere"),
            edge_modules=(EdgeModule(name="njs", objects=("js.so",), build=False),),
        ),
        AnsibleContribution(
            plugin="zulu",
            roles_path=Path("/nowhere"),
            edge_modules=(EdgeModule(name="njs", objects=("js.so",), build=True),),
        ),
    ]

    with pytest.raises(PluginError, match="declared differently"):
        resolve_edge_modules(contributions)


def test_one_shared_object_loaded_under_two_names_is_refused():
    """Nginx refuses a module loaded twice; this names both owners first."""
    contributions = [
        AnsibleContribution(
            plugin="alpha",
            roles_path=Path("/nowhere"),
            edge_modules=(EdgeModule(name="js", objects=("js.so",)),),
        ),
        AnsibleContribution(
            plugin="zulu",
            roles_path=Path("/nowhere"),
            edge_modules=(EdgeModule(name="njs", objects=("js.so",)),),
        ),
    ]

    with pytest.raises(PluginError, match="loaded by both"):
        resolve_edge_modules(contributions)


@pytest.mark.parametrize(
    "objects",
    [(), ("/usr/lib/nginx/modules/js.so",), ("../js.so",), ("js",)],
)
def test_a_module_names_a_plain_shared_object_file(objects):
    """The directory is core's half of the contract; the file is the plugin's."""
    with pytest.raises(ValueError):
        EdgeModule(name="njs", objects=objects)
