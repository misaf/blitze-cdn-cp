"""Installing an optional wheel, and uninstalling it again."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

#: The `uv` this developer or this CI job is actually running, resolved once
#: rather than spelled as a bare name on every call. A partial path would be
#: whatever `PATH` happened to hold when a subprocess started, and these
#: subprocesses build and install wheels.
from lifecycle_support import (
    DETACHABLE_SITE_PACKAGES,
    LIFECYCLE_CAPABILITY,
    Environment,
    _environment,
    _uv,
)
from paths import REPO_ROOT


def _declared_workspace_wheels(
    distribution: str, wheels: dict[str, Path]
) -> tuple[Path, ...]:
    """The wheels a distribution's own metadata says it needs beside `blitzecdn`.

    One package declares another today: `blitzecdn-certificates` runs
    `blitzecdn-origins`' play for the Automatic SSL/TLS scan. Resolved from the
    manifest rather than hard-coded, so the next declared edge is installed
    here without this file being edited — and so `uv pip install` really
    resolves the requirement instead of the test quietly working around it.
    """
    manifest = tomllib.loads(
        (REPO_ROOT / "packages" / distribution / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    names = [
        requirement.split(">")[0].split("[")[0].strip()
        for requirement in manifest["project"]["dependencies"]
    ]
    return tuple(wheels[name] for name in names if name != "blitzecdn")


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

    The second half is the one worth stating. A control plane that lost `dns`
    along with `backup`, or that reported the removed package as *rejected*
    rather than absent, would both pass a test that only checked the capability
    was gone.
    """
    after = detached.report()

    assert LIFECYCLE_CAPABILITY not in after["plugins"]
    assert LIFECYCLE_CAPABILITY not in after["capabilities"]
    assert LIFECYCLE_CAPABILITY not in after["commands"]
    assert after["rejected"] == []
    assert {"dns", "edges", "deployments"} <= set(after["plugins"])
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


@pytest.mark.parametrize(
    ("distribution", "capability", "overrides"),
    DETACHABLE_SITE_PACKAGES,
)
def test_site_capability_wheels_attach_and_detach_through_real_entry_points(
    tmp_path: Path,
    wheels: dict[str, Path],
    distribution: str,
    capability: str,
    overrides: dict[str, object],
):
    environment = _environment(tmp_path / "venv")
    environment.install(wheels["blitzecdn"])
    before = environment.report()
    assert capability not in before["capabilities"]
    assert environment.site_capabilities(overrides)["missing"] == [capability]

    environment.install(
        wheels[distribution], *_declared_workspace_wheels(distribution, wheels)
    )
    attached = environment.report()
    assert capability in attached["plugins"]
    assert capability in attached["capabilities"]
    assert attached["rejected"] == []
    assert environment.site_capabilities(overrides)["missing"] == []
    _uv("pip", "check", "--python", str(environment.python))

    if capability == "certificates":
        assert {"cert", "ssl"} <= set(attached["commands"])
        assert any("certificates" in path for path in attached["routes"])
        assert any("/ssl/automatic/" in path for path in attached["routes"])

    environment.uninstall(distribution)
    detached_report = environment.report()
    assert capability not in detached_report["plugins"]
    assert capability not in detached_report["capabilities"]
    assert detached_report["rejected"] == []
    assert detached_report["routes"]
    assert environment.site_capabilities(overrides)["missing"] == [capability]
    if capability == "certificates":
        assert not {"cert", "ssl"} & set(detached_report["commands"])
        assert not any("certificates" in path for path in detached_report["routes"])
        assert not any("/ssl/automatic/" in path for path in detached_report["routes"])


def test_configuration_requiring_an_absent_capability_fails_deterministically(
    core_only: Environment,
    tmp_path: Path,
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
    # The subject is plugin metadata, so the installation this command reads
    # must be one this test owns. Inherited, the project root is pytest's own
    # working directory — a developer's checkout — and the command opens, and
    # on a clean tree *creates*, the `.state/control-plane.db` beside it. Both
    # halves are wrong: writing into the working tree is a leak the gitignore
    # hides, and a database already there from another branch or an older
    # schema makes this run fail on migrations rather than on the capability
    # check. Naming the root moves every state path with it; the database is
    # named as well because it is the one file this test must not touch.
    environment["BLITZE_PROJECT_DIR"] = str(tmp_path)
    environment["BLITZE_DATABASE_PATH"] = str(tmp_path / "control-plane.db")
    finished = subprocess.run(
        [str(core_only.blitzecdn), "plugins"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=environment,
        timeout=300,
    )

    assert finished.returncode != 0
    assert "backup" in finished.stderr + finished.stdout
    assert "no installed plugin provides" in finished.stderr + finished.stdout
