"""The one wheel works outside the checkout, with no sibling distributions."""

import json
import os
import shutil
import subprocess
import sys

import pytest
from paths import REPO_ROOT

pytestmark = pytest.mark.packaging


@pytest.fixture(scope="module")
def installed_application(tmp_path_factory):
    uv = shutil.which("uv")
    assert uv, "the installed-application contract needs uv"
    directory = tmp_path_factory.mktemp("installed-application")
    output = directory / "dist"
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(output)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(output.glob("blitzecdn-*.whl"))
    environment = directory / "venv"
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = environment / "bin/python"
    # Reuse the exact locked runtime dependency set, without open-ended index
    # resolution or the editable application. Install the artifact itself last.
    subprocess.run(
        [uv, "sync", "--frozen", "--no-dev", "--no-install-project"],
        cwd=REPO_ROOT,
        env=os.environ | {"UV_PROJECT_ENVIRONMENT": str(environment)},
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    subprocess.run(
        [uv, "pip", "install", "--no-deps", "--python", str(python), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return directory, python, environment / "bin/blitzecdn"


def test_installed_wheel_carries_every_provider_command_and_resource(
    installed_application,
):
    directory, python, cli = installed_application
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("BLITZE_")
    }
    environment["BLITZE_PROJECT_DIR"] = str(directory / "project")
    environment.pop("PYTHONPATH", None)
    report = subprocess.run(
        [
            str(python),
            "-c",
            """
import json
from importlib.metadata import distributions
from blitzecdn.composition import load_registry
registry = load_registry()
print(json.dumps({
 "distributions": sorted(
     item.metadata["Name"] for item in distributions()
     if item.metadata["Name"].startswith("blitzecdn")
 ),
 "capabilities": sorted(registry.capabilities),
 "roles": [str(item.roles_path) for item in registry.ansible_contributions()],
 "templates": [str(item.templates_path) for item in registry.nginx_contributions()],
}))
""",
        ],
        cwd=directory,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(report.stdout)
    assert result["distributions"] == ["blitzecdn"]
    assert {
        "backup",
        "cache",
        "certificates",
        "http3",
        "geoip",
        "security",
        "origins",
    } <= set(result["capabilities"])
    for path in result["roles"] + result["templates"]:
        from pathlib import Path

        assert Path(path).is_dir()
        assert "site-packages/blitzecdn/" in path
        assert str(REPO_ROOT) not in path
    for command in ([], ["backup"], ["cache"], ["cert"], ["origin"]):
        described = subprocess.run(
            [str(cli), *command, "--help"],
            cwd=directory,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert described.returncode == 0, described.stderr
    assert not list((directory / "project").rglob("*.db"))


def test_installed_wheel_requires_explicit_schema_preparation(installed_application):
    directory, _, cli = installed_application
    project = directory / "schema-lifecycle"
    project.mkdir()
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("BLITZE_")
    }
    environment["BLITZE_PROJECT_DIR"] = str(project)
    environment.pop("PYTHONPATH", None)

    def invoke(*arguments):
        return subprocess.run(
            [str(cli), *arguments],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    refused = invoke("edge", "list", "--json")
    assert refused.returncode == 3
    assert "blitzecdn setup" in refused.stderr
    assert not list(project.iterdir())

    migrated = invoke("migrate")
    assert migrated.returncode == 0, migrated.stderr
    assert not (project / ".env").exists()
    assert json.loads(invoke("edge", "list", "--json").stdout) == []
    created = invoke(
        "edge",
        "add",
        "edge-one",
        "--host",
        "192.0.2.10",
        "--ssh-source",
        "198.51.100.8/32",
        "--json",
    )
    assert created.returncode == 0, created.stderr
    assert invoke("setup").returncode == 0
    credentials = (project / ".env").read_bytes()
    assert invoke("setup").returncode == 0
    assert (project / ".env").read_bytes() == credentials
    assert [
        edge["name"] for edge in json.loads(invoke("edge", "list", "--json").stdout)
    ] == ["edge-one"]
