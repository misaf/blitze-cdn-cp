"""Package discovery boundaries."""

from __future__ import annotations

import re
from importlib.metadata import entry_points
from pathlib import Path

import pytest
from package_boundary_support import (
    _BEHAVIOUR_LAYERS,
    _built_in_capabilities,
    _entry_point_names,
    _import_package,
    _manifest,
    _packages,
)
from paths import optional_packages

from blitzecdn.composition import load_control_plane_plugins
from blitzecdn.core.plugins import (
    ENTRY_POINT_GROUP,
)

# --- registration is metadata, and nothing else -----------------------------


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_an_optional_package_registers_through_the_shared_entry_point_group(
    package: Path,
):
    """One group, reused. Not a second registry, and not a list in core.

    `blitzecdn.plugins` already existed for a package this repository has never
    heard of, so a capability that moved *out* of the distribution uses the
    same door an external one always did — which is what makes the built-in
    and the third-party cases the same case.
    """
    manifest = _manifest(package)
    groups = manifest["project"]["entry-points"]
    assert list(groups) == [ENTRY_POINT_GROUP]
    targets = groups[ENTRY_POINT_GROUP]
    assert targets, f"{package.name} declares the group and advertises nothing"
    for value in targets.values():
        assert value.startswith(f"{_import_package(package)}.")
        assert value.endswith(".plugin")


def test_entry_point_names_are_unique_across_the_installed_environment():
    """Two distributions cannot both answer to one name.

    Checked against the environment rather than the manifests: the collision
    that matters is between what is *installed*, which may include a package
    from outside this workspace entirely.
    """
    names = [point.name for point in entry_points(group=ENTRY_POINT_GROUP)]
    assert sorted(names) == sorted(set(names))


def test_the_installed_environment_advertises_every_workspace_package():
    """The manifests and the environment agree.

    A test that only read `pyproject.toml` would pass on a package that was
    never installed, and a package that is not installed contributes nothing —
    so this asserts the metadata `importlib` actually reports, which is the
    same read `register_external` performs.
    """
    advertised = {point.name for point in entry_points(group=ENTRY_POINT_GROUP)}
    expected = {
        name
        for package in _packages()
        for name in _manifest(package)["project"]["entry-points"][ENTRY_POINT_GROUP]
    }
    assert expected <= advertised


def test_an_optional_capability_is_discovered_only_through_its_entry_point():
    """The property the whole boundary exists for, stated as one assertion.

    Discovery with the group switched off is exactly the built-in set. Every
    capability an optional distribution supplies is absent from it and present
    with it, and nothing in core was consulted either way.
    """
    builtins = load_control_plane_plugins(entry_point_group=None)
    installed = load_control_plane_plugins()

    added = installed.capabilities - builtins.capabilities
    assert added, "no optional capability is installed in this environment"
    assert {"backup", "cache"} <= added
    assert not (added & builtins.capabilities)


def _contract_only_capabilities() -> list[Path]:
    """A capability that decides nothing: a policy, and no service or api."""
    return [
        path
        for path in _built_in_capabilities()
        if (path / "policy.py").is_file()
        and not any((path / layer).is_dir() for layer in _BEHAVIOUR_LAYERS)
    ]


def test_the_contract_capabilities_agree_on_who_their_siblings_are():
    """Five docstrings each name the other four, and they used to disagree.

    "The same split as ..." is a cross-reference a reader follows to check they
    have understood the arrangement, so a list that is missing a name teaches
    the wrong shape. Three of the five carried the sentence, one of those three
    omitted `security`, and the other two never had it — which is what a list
    kept by hand in five files does.

    Derived rather than declared: the set is every capability that has a
    `policy.py` and neither of the behaviour layers, so a sixth contract
    capability makes all six docstrings fail until each names the other five,
    and a contract that grows a `service/` drops out of all of them.
    """
    names = {path.name for path in _contract_only_capabilities()}
    assert names == {"cache", "compression", "http", "security", "tls"}, (
        "the contract capabilities moved; the docstrings below are the map"
    )
    wrong = {}
    for path in _contract_only_capabilities():
        document = (path / "__init__.py").read_text(encoding="utf-8")
        sentence = re.search(r"The same split as (.+?), for", document, re.S)
        named = (
            set(re.findall(r"``([^`]+)``", sentence.group(1))) if sentence else set()
        )
        if named != names - {path.name}:
            wrong[path.name] = sorted(named)
    assert wrong == {}


def test_a_capability_registers_unless_a_wheel_already_claims_its_name():
    """Why nine of the twelve directories hold a `plugin.py` and three do not.

    The split does not follow contract-versus-implementation, which is what a
    reader guesses and what this file's map used to leave them guessing: `http`
    and `tls` are contract capabilities that register anyway.

    It follows the name. A plugin name is unique across everything installed,
    and `capability_requirements` is written in those names — a site with
    `cache_enabled` requires `cache`. The wheels implementing `cache`,
    `compression` and `security` register under exactly those names, so core
    registering them too would be the duplicate discovery refuses. `tls` and
    `http` are free because their wheels are named `certificates` and `http3`.

    Derived from `packages/` rather than listed, so a new wheel that takes a
    capability's own name fails here until that capability stops registering —
    which is the conversation worth having, and it is a startup error otherwise.
    """
    claimed = {
        name for package in optional_packages() for name in _entry_point_names(package)
    }
    for capability in _built_in_capabilities():
        registers = (capability / "plugin.py").is_file()
        assert registers != (capability.name in claimed), (
            f"{capability.name} "
            f"{'registers' if registers else 'does not register'} and a wheel "
            f"{'claims' if capability.name in claimed else 'does not claim'} "
            "its name"
        )
