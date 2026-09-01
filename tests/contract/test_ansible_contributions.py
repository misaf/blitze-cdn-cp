"""The role search path: composed from core and from what is installed.

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

from blitzecdn.core import ansible
from blitzecdn.core.ansible import execution as ansible_execution
from blitzecdn.core.ansible.roles import resolve_role_search_path
from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.plugins import AnsibleContribution


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
        FakeEdgeStore(),
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
        ansible.AnsibleRunner(settings, FakeEdgeStore()).run(check=False)

    assert captured["ANSIBLE_ROLES_PATH"] == str(settings.ansible_dir / "roles")
