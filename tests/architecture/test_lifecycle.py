"""Attaching and detaching a capability, through real Python packaging.

Everything else in the suite asserts against the source tree or against the
environment this run happens to have. This file builds real wheels, installs
them into throwaway virtualenvs, and asks the control plane what it can do —
because the property being claimed is about `pip install`, and a test that
mocked the registry would prove only that the mock was written correctly.

Three environments are built, each once per session:

* **core only** — the root wheel and nothing else. This is the configuration
  the acceptance criteria call "BlitzeCDN root package works alone", and it is
  also what proves the root wheel does not drag an optional distribution in
  behind it.
* **attached** — core plus one optional wheel, which must make that
  capability's metadata, routes and commands appear.
* **detached** — the attached environment with the optional wheel uninstalled
  again, which must return it to the first.

They are slow — a build and an install each — so they are marked `packaging`
and the whole module is skipped when the toolchain is not available.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from paths import REPO_ROOT, optional_packages

#: The `uv` this developer or this CI job is actually running, resolved once
#: rather than spelled as a bare name on every call. A partial path would be
#: whatever `PATH` happened to hold when a subprocess started, and these
#: subprocesses build and install wheels.
UV = shutil.which("uv")

pytestmark = [
    pytest.mark.packaging,
    pytest.mark.skipif(UV is None, reason="packaging lifecycle needs the uv CLI"),
]

#: The capability used for the attach/detach cycle. One is enough: the
#: mechanism is the same for every optional distribution, and building a wheel
#: and two environments per package would multiply the slowest tests in the
#: suite for no additional property. `backup` is chosen because it is the
#: package with no runtime dependency on anything but `Settings`, so a failure
#: here is a failure of the *packaging*, never of the capability.
LIFECYCLE_PACKAGE = "blitzecdn-backup"
LIFECYCLE_CAPABILITY = "backup"


def _uv(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    # Never let the developer's own virtualenv answer for the one under test.
    environment.pop("VIRTUAL_ENV", None)
    return subprocess.run(
        [str(UV), *arguments],
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env=environment,
        timeout=900,
    )


@dataclass(frozen=True)
class Environment:
    """A throwaway virtualenv, and how to ask the control plane inside it."""

    root: Path

    @property
    def python(self) -> Path:
        return self.root / "bin" / "python"

    @property
    def blitzecdn(self) -> Path:
        return self.root / "bin" / "blitzecdn"

    def install(self, *wheels: Path) -> None:
        _uv("pip", "install", "--python", str(self.python), *map(str, wheels))

    def uninstall(self, *distributions: str) -> None:
        _uv("pip", "uninstall", "--python", str(self.python), *distributions)

    def report(self) -> dict[str, object]:
        """What the control plane in this environment says it consists of.

        Asked over a subprocess and answered as JSON, because the point is the
        *other* interpreter: importing the installed package into this one
        would read the workspace's own source tree through `sys.path` and
        report on an environment that is not the one being tested.
        """
        program = (
            "import json;"
            "from blitzecdn.core.plugins import load_plugins;"
            "r = load_plugins();"
            "print(json.dumps({"
            "'plugins': sorted(p.name for p in r.plugins),"
            "'capabilities': sorted(r.capabilities),"
            "'rejected': [str(x) for x in r.rejected],"
            "'commands': sorted("
            "  g.name or '' for g in r.cli_commands()),"
            "'routes': sorted("
            "  route.path for router in r.api_routers()"
            "  for route in router.routes if hasattr(route, 'path')),"
            "}))"
        )
        finished = subprocess.run(
            [str(self.python), "-c", program],
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
        )
        return json.loads(finished.stdout)


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


def _environment(root: Path) -> Environment:
    _uv(
        "venv",
        "--python",
        f"{sys.version_info.major}.{sys.version_info.minor}",
        str(root),
    )
    return Environment(root)


@pytest.fixture(scope="session")
def core_only(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
) -> Environment:
    environment = _environment(tmp_path_factory.mktemp("core-only") / "venv")
    environment.install(wheels["blitzecdn"])
    return environment


# --- core alone -------------------------------------------------------------


def test_the_root_wheel_installs_no_optional_capability(core_only: Environment):
    """`pip install blitzecdn` is the control plane and nothing more.

    The extras in `[project.optional-dependencies]` name these distributions,
    which is how `blitzecdn[all]` works — and an extra a caller did not ask for
    must install nothing. If it did, "detached" would be unreachable from a
    normal install and the whole boundary would be decorative.
    """
    installed = _uv(
        "pip", "list", "--python", str(core_only.python), "--format", "json"
    )
    names = {entry["name"] for entry in json.loads(installed.stdout)}

    assert "blitzecdn" in names
    assert not {package.name for package in optional_packages()} & names


def test_core_alone_starts_and_registers_every_required_capability(
    core_only: Environment,
):
    """The control plane is coherent with no optional distribution present.

    Not "it imports": it discovers its plugins, builds its command tree and its
    router set, and reports nothing rejected. A required capability that had
    quietly moved out would be missing here rather than merely absent from a
    list somebody maintains.
    """
    report = core_only.report()

    assert report["rejected"] == []
    assert {"sites", "dns", "edges", "deployments", "tls", "diagnostics"} <= set(
        report["plugins"]  # type: ignore[arg-type]
    )
    assert report["commands"]
    assert report["routes"]


def test_core_alone_offers_no_optional_capability(core_only: Environment):
    report = core_only.report()

    assert LIFECYCLE_CAPABILITY not in report["capabilities"]
    assert LIFECYCLE_CAPABILITY not in report["commands"]
    assert "cache" not in report["capabilities"]
    assert not [path for path in report["routes"] if "cache" in path]  # type: ignore[union-attr]


def test_the_api_and_the_cli_both_start_with_no_optional_package(
    core_only: Environment,
):
    """Both entry points compose from the registry, so both are worth asking.

    `--help` renders the whole command tree and `create_app` includes every
    contributed router, so a capability that core still expected to be there
    would fail here rather than at the first request for it.
    """
    finished = subprocess.run(
        [str(core_only.blitzecdn), "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    assert "deploy" in finished.stdout
    assert "backup" not in finished.stdout

    subprocess.run(
        [
            str(core_only.python),
            "-c",
            "from blitzecdn.api import create_app;"
            "from blitzecdn.core.config import Settings;"
            "create_app(Settings.from_environment()).openapi()",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )


# --- attach, then detach ----------------------------------------------------


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


def test_installing_a_distribution_makes_its_capability_appear(
    attached: Environment,
):
    """Attach: one `pip install`, and the capability is there.

    Nothing else changes. No line of core is edited, no registry is told, and
    the process is not even restarted with different arguments — the next one
    to start reads the installed metadata and finds an entry point that was not
    there before. The fixture asserts the capability was absent beforehand.
    """
    report = attached.report()

    assert LIFECYCLE_CAPABILITY in report["plugins"]
    assert LIFECYCLE_CAPABILITY in report["capabilities"]
    assert LIFECYCLE_CAPABILITY in report["commands"]
    assert report["rejected"] == []


def test_the_attached_capability_reaches_the_command_line(attached: Environment):
    """It is on the real command tree, not merely in a registry listing."""
    finished = subprocess.run(
        [str(attached.blitzecdn), "backup", "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    assert "create" in finished.stdout
    assert "restore" in finished.stdout


def test_an_optional_distribution_accepts_the_installed_core(attached: Environment):
    """Its declared dependency is satisfied by the core that is present.

    `uv pip check` is the honest form of this: it reads the installed metadata
    and reports an unsatisfied or conflicting requirement, which is what a
    version range that did not actually admit this core would produce.
    """
    _uv("pip", "check", "--python", str(attached.python))


def test_uninstalling_a_distribution_makes_its_capability_disappear(
    detached: Environment,
):
    """Detach: the capability goes, and everything else keeps working.

    The second half is the one worth stating. A control plane that lost `sites`
    along with `backup`, or that reported the removed package as *rejected*
    rather than absent, would both pass a test that only checked the capability
    was gone.
    """
    after = detached.report()

    assert LIFECYCLE_CAPABILITY not in after["plugins"]
    assert LIFECYCLE_CAPABILITY not in after["capabilities"]
    assert LIFECYCLE_CAPABILITY not in after["commands"]
    assert after["rejected"] == []
    assert {"sites", "dns", "edges", "deployments"} <= set(
        after["plugins"]  # type: ignore[arg-type]
    )
    assert after["routes"]


def test_the_cli_still_works_after_the_capability_is_detached(detached: Environment):
    finished = subprocess.run(
        [str(detached.blitzecdn), "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    assert "deploy" in finished.stdout
    assert "backup" not in finished.stdout


# --- configuration that names a capability nothing supplies -----------------


def test_configuration_requiring_an_absent_capability_fails_deterministically(
    core_only: Environment,
):
    """The deliberate half of "the package is not installed".

    Absence on its own is normal and silent — detaching is a supported
    operation. An installation that has *declared* it depends on a capability
    is a different case, and it refuses to start with the token named rather
    than coming up and behaving as though the capability had been configured
    off. Nothing in the path knows what `backup` is: the token comes from
    configuration and the answer from plugin metadata.
    """
    environment = dict(os.environ)
    environment.pop("VIRTUAL_ENV", None)
    environment["BLITZE_REQUIRED_CAPABILITIES"] = "backup"
    finished = subprocess.run(
        [str(core_only.blitzecdn), "doctor"],
        capture_output=True,
        text=True,
        env=environment,
        timeout=300,
    )

    assert finished.returncode != 0
    assert "backup" in finished.stderr + finished.stdout
    assert "no installed plugin provides" in finished.stderr + finished.stdout
