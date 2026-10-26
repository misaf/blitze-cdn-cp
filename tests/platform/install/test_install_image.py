"""The control-plane image: how a server gets it, and how it runs Ansible.

A standalone server installs Docker, obtains the release's image and runs
everything else from it, so the host never resolves a Python package. These
drive the image helpers against a stubbed engine and hold the bootstrap's
Docker files to the role that later converges them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml
from support.install import BASH, SCRIPT, _function, _workspace_version
from support.paths import CORE_ANSIBLE, REPO_ROOT

from blitzecdn.assets.docker import CONTROL_PLANE_DOCKERFILE

DOCKER_ROLE = CORE_ANSIBLE / "roles/blitzecdn_docker"
CONTROL_PLANE_ROLE = CORE_ANSIBLE / "roles/blitzecdn_controlplane"
WORKFLOW = REPO_ROOT / ".github/workflows/control-plane-image.yml"


def _bash(
    expression: str,
    *,
    path: Path | None = None,
    env: dict[str, str] | None = None,
    script: Path = SCRIPT,
) -> subprocess.CompletedProcess[str]:
    """Source the installer and run one expression, output kept byte for byte."""
    environment = os.environ.copy()
    if path is not None:
        environment["PATH"] = f"{path}:{environment['PATH']}"
    environment.update(env or {})
    return subprocess.run(  # noqa: S603 - fixed executable and repository script
        [BASH, "-c", f'source "{script}"\n{expression}'],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _render(template: Path, **variables: object) -> str:
    """Render a role template the way Ansible's template module does.

    Ansible keeps the source's trailing newline, which plain Jinja drops.
    """
    environment = jinja2.Environment(  # noqa: S701 - renders configuration files
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
    )
    environment.filters["bool"] = bool
    return environment.from_string(template.read_text(encoding="utf-8")).render(
        **variables
    )


# --- Docker, seeded exactly as the role would leave it ------------------------


def test_the_bootstrap_repository_is_the_one_the_role_renders():
    """A difference would only cost a rewrite, but it would also be a lie.

    The play composes the repository URL from the distribution, as the
    bootstrap does from /etc/os-release.
    """
    play = yaml.safe_load(
        (CORE_ANSIBLE / "playbooks/control-plane.yml").read_text(encoding="utf-8")
    )
    docker_vars = next(
        role["vars"] for role in play[0]["roles"] if role["role"] == "blitzecdn_docker"
    )
    defaults = yaml.safe_load(
        (DOCKER_ROLE / "defaults/main.yml").read_text(encoding="utf-8")
    )
    for distribution, codename, architecture in (
        ("Ubuntu", "resolute", "amd64"),
        ("Debian", "trixie", "arm64"),
    ):
        facts = {"distribution": distribution, "distribution_release": codename}
        url = _render_inline(docker_vars["blitzecdn_docker_repository_url"], facts)
        expected = _render(
            DOCKER_ROLE / "templates/docker.sources.j2",
            ansible_facts=facts,
            blitzecdn_docker_repository_url=url,
            blitzecdn_docker_architecture=architecture,
            blitzecdn_docker_keyring=defaults["blitzecdn_docker_keyring"],
        )
        result = _bash(
            f"docker_sources {distribution.lower()} {codename} {architecture}"
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected


def _render_inline(expression: str, facts: dict[str, str]) -> str:
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)  # noqa: S701
    return environment.from_string(expression.strip()).render(ansible_facts=facts)


def test_the_bootstrap_daemon_configuration_is_the_one_the_role_renders():
    """Byte for byte, or the converge restarts the engine under its own Ansible.

    The play runs in a container on this daemon, and a changed daemon.json
    notifies a restart.
    """
    defaults = yaml.safe_load(
        (DOCKER_ROLE / "defaults/main.yml").read_text(encoding="utf-8")
    )
    expected = _render(DOCKER_ROLE / "templates/daemon.json.j2", **defaults)
    result = _bash("docker_daemon_json")
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
    json.loads(result.stdout)


def test_the_bootstrap_installs_the_packages_the_role_requires():
    defaults = yaml.safe_load(
        (DOCKER_ROLE / "defaults/main.yml").read_text(encoding="utf-8")
    )
    install = _function("ensure_docker")
    line = re.search(r"apt-get install -y ([^\n]*\\\n[^\n]*)", install)
    assert line is not None
    assert (
        line.group(1).replace("\\\n", " ").split()
        == defaults["blitzecdn_docker_packages"]
    )
    # Restarted here, before any container of ours runs on the engine.
    assert install.index("converge_host_file /etc/docker/daemon.json") < install.index(
        "systemctl restart docker"
    )


# --- the pin and the workflow that publishes it -------------------------------


def test_the_role_pins_the_published_image_of_its_own_release():
    """`just release-version` moves this with the version, and the tag publishes it."""
    defaults = yaml.safe_load(
        (CONTROL_PLANE_ROLE / "defaults/main.yml").read_text(encoding="utf-8")
    )
    assert defaults["blitzecdn_controlplane_image"] == (
        f"ghcr.io/misaf/blitzecdn-control-plane:{_workspace_version()}"
    )
    result = _bash("pinned_control_plane_image")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == defaults["blitzecdn_controlplane_image"]


def test_the_workflow_publishes_the_pinned_repository_from_the_shipped_dockerfile():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["image"]["steps"]
    meta = next(step for step in steps if step.get("id") == "meta")
    build = next(step for step in steps if step.get("id") == "build")
    assert meta["with"]["images"] == (
        "ghcr.io/${{ github.repository_owner }}/blitzecdn-control-plane"
    )
    # A semver tag, so the pin's `:6.0.0` exists; never `latest`.
    assert "type=semver,pattern={{version}}" in meta["with"]["tags"]
    assert "latest" not in meta["with"]["tags"]
    assert build["with"]["file"] == str(CONTROL_PLANE_DOCKERFILE.relative_to(REPO_ROOT))
    assert build["with"]["context"] == "."
    # Release tags publish; a pull request only builds.
    assert workflow["on"]["push"] == {"tags": ["v*.*.*"]}
    assert build["with"]["push"] == "${{ github.event_name != 'pull_request' }}"


def test_a_source_build_uses_the_dockerfile_the_package_ships():
    script = SCRIPT.read_text(encoding="utf-8")
    assert (
        f"readonly CONTROL_PLANE_DOCKERFILE="
        f"{CONTROL_PLANE_DOCKERFILE.relative_to(REPO_ROOT)}\n"
    ) in script


# --- obtaining the image -------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> Path:
    """A docker stub: images listed in `present` exist, every call is logged.

    `pull` adds the image unless PULL_FAILS is set; `load` adds LOADED_IMAGE.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "present").touch()
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'state="{tmp_path}"\n'
        'printf "%s\\n" "$*" >> "${state}/calls"\n'
        'image="${*: -1}"\n'
        'case "$1 $2" in\n'
        '  "image inspect")\n'
        '    grep -qxF "${image}" "${state}/present" || exit 1\n'
        '    if [[ "$*" == *RepoDigests* ]]; then\n'
        '      printf "%s\\n" "${REPO_DIGESTS:-}"\n'
        "    fi\n"
        "    exit 0 ;;\n"
        "  pull*)\n"
        '    [[ -z "${PULL_FAILS:-}" ]] || exit 1\n'
        '    printf "%s\\n" "${image}" >> "${state}/present"; exit 0 ;;\n'
        "  load*)\n"
        '    printf "%s\\n" "${LOADED_IMAGE}" >> "${state}/present"; exit 0 ;;\n'
        "  build*)\n"
        "    exit 0 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bindir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    for stub in bindir.iterdir():
        stub.chmod(0o755)
    return tmp_path


def _calls(engine: Path) -> list[str]:
    calls = engine / "calls"
    return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


def _pin() -> str:
    return f"ghcr.io/misaf/blitzecdn-control-plane:{_workspace_version()}"


def test_the_pinned_image_is_pulled_and_run_by_its_registry_digest(engine: Path):
    digest = "sha256:" + "a" * 64
    result = _bash(
        "obtain_control_plane_image",
        path=engine / "bin",
        env={
            "REPO_DIGESTS": (
                f"mirror.example/blitzecdn-control-plane@sha256:{'b' * 64}\n"
                f"ghcr.io/misaf/blitzecdn-control-plane@{digest}"
            )
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_pin()}@{digest}\n"
    assert f"pull {_pin()}" in _calls(engine)


def test_an_image_already_present_is_not_pulled_again(engine: Path):
    """A re-run, or a host whose operator loaded the image by hand."""
    (engine / "present").write_text(f"{_pin()}\n", encoding="utf-8")

    result = _bash("obtain_control_plane_image", path=engine / "bin")

    assert result.returncode == 0, result.stderr
    # No registry digest for a loaded image: the reference is kept as given.
    assert result.stdout == f"{_pin()}\n"
    assert not any(call.startswith("pull") for call in _calls(engine))


def test_an_operator_can_name_a_different_image(engine: Path):
    pinned = f"{_pin()}@sha256:{'c' * 64}"

    result = _bash(
        "obtain_control_plane_image",
        path=engine / "bin",
        env={"BLITZECDN_CONTROL_PLANE_IMAGE": pinned},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{pinned}\n"
    assert f"pull {pinned}" in _calls(engine)


def test_a_failed_pull_is_retried_and_then_names_both_fallbacks(engine: Path):
    result = _bash(
        "obtain_control_plane_image", path=engine / "bin", env={"PULL_FAILS": "1"}
    )

    assert result.returncode == 1
    assert sum(call.startswith("pull") for call in _calls(engine)) == 3
    assert "BLITZECDN_CONTROL_PLANE_ARCHIVE" in result.stderr
    assert "BLITZECDN_CONTROL_PLANE_BUILD=1" in result.stderr
    assert result.stdout == ""


def test_an_archive_is_loaded_instead_of_pulled(engine: Path, tmp_path: Path):
    archive = tmp_path / "control-plane.tar"
    archive.touch()

    result = _bash(
        "obtain_control_plane_image",
        path=engine / "bin",
        env={"BLITZECDN_CONTROL_PLANE_ARCHIVE": str(archive), "LOADED_IMAGE": _pin()},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_pin()}\n"
    assert f"load --input {archive}" in _calls(engine)
    assert not any(call.startswith("pull") for call in _calls(engine))


def test_an_archive_without_the_expected_image_is_refused(engine: Path, tmp_path: Path):
    archive = tmp_path / "control-plane.tar"
    archive.touch()

    result = _bash(
        "obtain_control_plane_image",
        path=engine / "bin",
        env={
            "BLITZECDN_CONTROL_PLANE_ARCHIVE": str(archive),
            "LOADED_IMAGE": "something-else:1",
        },
    )

    assert result.returncode == 1
    assert f"does not contain {_pin()}" in result.stderr


def test_the_source_fallback_builds_this_checkout_and_pulls_nothing(engine: Path):
    result = _bash(
        "obtain_control_plane_image",
        path=engine / "bin",
        env={"BLITZECDN_CONTROL_PLANE_BUILD": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "blitzecdn-control-plane:local\n"
    builds = [call for call in _calls(engine) if call.startswith("build")]
    assert builds == [
        f"build --pull --file {CONTROL_PLANE_DOCKERFILE} "
        f"--tag blitzecdn-control-plane:local {REPO_ROOT}"
    ]
    assert not any(call.startswith("pull") for call in _calls(engine))


# --- running Ansible from the image -------------------------------------------


def test_every_play_runs_in_the_image_against_the_host_through_nsenter():
    """The plays and their Ansible are the image's; the tasks run on the host.

    nsenter needs a privileged container in the host's PID namespace, and the
    plays name the connection themselves so nothing else can run them locally
    inside the container by mistake.
    """
    body = _function("run_playbook")
    for flag in ("--privileged", "--pid=host", "--network=host", "--user 0:0"):
        assert flag in body
    assert '"${IMAGE_ANSIBLE_DIR}/playbooks/${playbook}"' in body
    for playbook in ("control-plane.yml", "uninstall.yml"):
        play = yaml.safe_load(
            (CORE_ANSIBLE / "playbooks" / playbook).read_text(encoding="utf-8")
        )
        assert play[0]["connection"] == "community.docker.nsenter"
        # Already root; `become` would run sudo, which a bare host lacks.
        assert play[0]["become"] is False
        assert play[0]["hosts"] == "localhost"
    requirements = yaml.safe_load(
        (CORE_ANSIBLE / "requirements.yml").read_text(encoding="utf-8")
    )
    assert "community.docker" in {item["name"] for item in requirements["collections"]}


def test_the_image_carries_the_playbooks_where_the_installer_runs_them():
    script = SCRIPT.read_text(encoding="utf-8")
    assert (
        "readonly IMAGE_ANSIBLE_DIR=/opt/blitzecdn/src/blitzecdn/assets/ansible\n"
        in (script)
    )
    dockerfile = CONTROL_PLANE_DOCKERFILE.read_text(encoding="utf-8")
    assert "WORKDIR /opt/blitzecdn\nCOPY . /opt/blitzecdn\n" in dockerfile


def test_the_installed_image_is_read_back_from_the_rendered_compose_project(
    tmp_path: Path,
):
    """Uninstall runs the image the services run; the role's file is the record."""
    environment = jinja2.Environment(  # noqa: S701 - renders YAML, not HTML
        undefined=jinja2.StrictUndefined
    )
    environment.filters["to_json"] = json.dumps
    image = f"{_pin()}@sha256:{'d' * 64}"
    compose = tmp_path / "control-plane.compose.yml"
    compose.write_text(
        environment.from_string(
            (CONTROL_PLANE_ROLE / "templates/compose.yml.j2").read_text(
                encoding="utf-8"
            )
        ).render(
            blitzecdn_controlplane_compose_project="blitzecdn-control-plane",
            blitzecdn_controlplane_image=image,
            blitzecdn_controlplane_config_dir="/etc/blitzecdn",
            blitzecdn_controlplane_state_dir="/var/lib/blitzecdn",
            blitzecdn_controlplane_backup_dir="/var/backups/blitzecdn",
        ),
        encoding="utf-8",
    )
    script = tmp_path / "install.sh"
    script.write_text(
        SCRIPT.read_text(encoding="utf-8").replace(
            "/etc/blitzecdn/control-plane.compose.yml", str(compose)
        ),
        encoding="utf-8",
    )

    result = _bash("installed_control_plane_image", script=script)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{image}\n"
