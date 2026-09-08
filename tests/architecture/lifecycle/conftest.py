"""Environments and marks for the packaging lifecycle suite.

The three standing environments are session-scoped: each is a real build and a
real install, so they are created once and shared by every module here.
Fixtures have to be registered with pytest rather than imported, which is why
they live here and not in ``lifecycle_support.py``.

The marks are applied from here too, rather than as a ``pytestmark`` repeated
in each module. Every test in this directory builds wheels and installs them,
so a module added later is marked by being here — there is no line for anyone
to forget.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lifecycle_support import (
    LIFECYCLE_CAPABILITY,
    LIFECYCLE_PACKAGE,
    UV,
    Environment,
    _environment,
    _uv,
)
from paths import optional_packages


def pytest_collection_modifyitems(items):
    """Mark everything collected *here* `packaging`, and skip it without uv.

    A conftest hook is handed the whole session's items, not only the ones
    beneath it, so the directory test is what keeps this from marking the rest
    of the suite as packaging and deselecting all of it from every fast run.
    """
    here = Path(__file__).parent
    skip = pytest.mark.skipif(UV is None, reason="packaging lifecycle needs the uv CLI")
    for item in items:
        if Path(str(item.path)).is_relative_to(here):
            item.add_marker(pytest.mark.packaging)
            item.add_marker(skip)


@pytest.fixture(scope="session")
def wheels(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Every distribution in the workspace, built.

    Building them all rather than only the two used below is deliberate: "all
    distributions build successfully" is itself one of the things being
    claimed, and a package that no longer builds should fail here rather than
    in a release.
    """
    output = tmp_path_factory.mktemp("wheels")
    _uv("build", "--wheel", "--out-dir", str(output))
    for package in optional_packages():
        _uv("build", "--wheel", "--package", package.name, "--out-dir", str(output))
    built = {
        path.name.split("-")[0].replace("_", "-"): path for path in output.glob("*.whl")
    }
    expected = {"blitzecdn", *(package.name for package in optional_packages())}
    assert expected <= set(built), f"missing wheels: {expected - set(built)}"
    return built


@pytest.fixture(scope="session")
def core_only(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
) -> Environment:
    environment = _environment(tmp_path_factory.mktemp("core-only") / "venv")
    environment.install(wheels["blitzecdn"])
    return environment


@pytest.fixture(scope="session")
def attached(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
) -> Environment:
    """Core with one optional distribution installed, and the state before it.

    A fixture rather than a test that later tests build on, because the suite
    runs across workers: an assertion that depended on another test having
    already installed something would pass or fail on how the cases happened to
    be distributed. Each environment here is complete on its own.
    """
    environment = _environment(tmp_path_factory.mktemp("attached") / "venv")
    environment.install(wheels["blitzecdn"])
    before = environment.report()
    assert LIFECYCLE_CAPABILITY not in before["capabilities"], (
        "the core wheel installed an optional distribution"
    )
    environment.install(wheels[LIFECYCLE_PACKAGE])
    return environment


@pytest.fixture(scope="session")
def detached(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
) -> Environment:
    """The full round trip: core, attach, detach. Its own environment.

    Deliberately not the `attached` one with the package removed afterwards —
    that would make one fixture's state depend on another fixture's teardown
    order, which is the same fragility in a different place.
    """
    environment = _environment(tmp_path_factory.mktemp("detached") / "venv")
    environment.install(wheels["blitzecdn"], wheels[LIFECYCLE_PACKAGE])
    assert LIFECYCLE_CAPABILITY in environment.report()["capabilities"]
    environment.uninstall(LIFECYCLE_PACKAGE)
    return environment
