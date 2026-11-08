"""Scripts Ansible copies to edges run under the edge Python floor.

The control plane requires Python 3.15.2, but it only runs inside the
control-plane image. The standalone standard-library scripts under a role's
`files/` are copied to an edge and executed by the edge host's own `python3`,
so they must keep working on the edge Python floor documented in
`src/blitzecdn/assets/ansible/README.md`. Ruff lints them with the matching
`per-file-target-version`; this runs them under that interpreter, which also
catches standard-library APIs no linter target knows about.

The floor interpreter comes from uv. Locally the test skips when uv has no
3.12; CI installs one and sets `BLITZE_REQUIRE_EDGE_PYTHON` so a missing
interpreter fails instead of quietly passing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EDGE_PYTHON = "3.12"
EDGE_TARGET = "py312"


def _edge_script_glob() -> str:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    targets: dict[str, str] = pyproject["tool"]["ruff"]["per-file-target-version"]
    globs = [glob for glob, target in targets.items() if target == EDGE_TARGET]
    assert len(globs) == 1, targets
    return globs[0]


EDGE_SCRIPTS = sorted(ROOT.glob(_edge_script_glob()))


def _edge_interpreter() -> str:
    uv = shutil.which("uv")
    found = (
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [uv, "python", "find", "--no-project", EDGE_PYTHON],
            capture_output=True,
            text=True,
            check=False,
        )
        if uv
        else None
    )
    if found is None or found.returncode != 0:
        reason = f"uv has no Python {EDGE_PYTHON} (uv python install {EDGE_PYTHON})"
        if os.environ.get("BLITZE_REQUIRE_EDGE_PYTHON"):
            pytest.fail(reason)
        pytest.skip(reason)
    return found.stdout.strip()


def test_the_edge_target_covers_the_scripts_ansible_copies_to_edges():
    names = {script.name for script in EDGE_SCRIPTS}
    assert {"collect_traffic.py", "configuration_release.py"} <= names
    # Every Python file a role ships to a host is one of them.
    shipped = {
        path
        for path in (ROOT / "src").rglob("*.py")
        if path.parent.name == "files" and "roles" in path.parts
    }
    assert shipped == set(EDGE_SCRIPTS)


@pytest.mark.parametrize("script", EDGE_SCRIPTS, ids=lambda path: path.name)
def test_edge_script_loads_under_the_edge_python_floor(script: Path):
    interpreter = _edge_interpreter()
    # `-I` keeps the checkout and any PYTHON* variables out of the import path,
    # as on an edge where the control-plane package is not installed. Loading
    # the module compiles it and resolves every import and top-level definition
    # without running its entry point.
    result = subprocess.run(  # noqa: S603 - fixed argv over a repository file
        [
            interpreter,
            "-I",
            "-c",
            "import runpy, sys; "
            "assert sys.version_info[:2] == tuple(map(int, sys.argv[2].split('.'))); "
            "runpy.run_path(sys.argv[1], run_name='edge_python_floor')",
            str(script),
            EDGE_PYTHON,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
