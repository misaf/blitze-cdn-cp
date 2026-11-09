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
import shutil
import subprocess
from pathlib import Path

import installer_sync
import jinja2
import pytest
import yaml
from support.install import BASH, SCRIPT, script_function, workspace_version
from support.paths import CORE_ANSIBLE, FIXTURES, REPO_ROOT

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


def test_the_bootstrap_carries_the_roles_current_docker_setup():
    """Byte for byte, or the converge restarts the engine under its own Ansible.

    The play runs in a container on this daemon, and a changed daemon.json
    notifies a restart. The block is generated, so this only catches a role
    edit that was never followed by `just installer-sync`.
    """
    assert installer_sync.synced(SCRIPT.read_text(encoding="utf-8")) == (
        SCRIPT.read_text(encoding="utf-8")
    ), "install.sh is stale; run `just installer-sync`"
    result = _bash("docker_daemon_json")
    assert result.returncode == 0, result.stderr
    json.loads(result.stdout)


def test_the_bootstrap_restarts_a_reconfigured_daemon_before_using_it():
    install = script_function("ensure_docker")
    assert 'apt-get install -y "${DOCKER_PACKAGES[@]}"' in install
    # Restarted here, before any container of ours runs on the engine.
    assert install.index(
        'converge_host_file "${DOCKER_DAEMON_CONFIG_FILE}"'
    ) < install.index("systemctl restart docker")


@pytest.mark.skipif(shutil.which("gpg") is None, reason="needs gpg")
@pytest.mark.parametrize(
    ("keys", "trusted"),
    [
        (["docker.asc"], True),
        # apt would trust the second key for Docker's repository too.
        (["docker.asc", "other.asc"], False),
        (["other.asc"], False),
        ([], False),
    ],
)
def test_only_dockers_own_apt_key_is_installed(
    tmp_path: Path, keys: list[str], trusted: bool
):
    key = tmp_path / "gpg"
    key.write_text(
        "".join(
            (FIXTURES / "apt-keys" / name).read_text(encoding="utf-8") for name in keys
        ),
        encoding="utf-8",
    )

    result = _bash(f'docker_key_trusted "{key}"')

    assert (result.returncode == 0) is trusted, result.stderr


def test_the_bootstrap_and_the_role_trust_the_same_docker_key():
    """The fingerprint is the role's, carried by `just installer-sync`."""
    defaults = yaml.safe_load(
        (DOCKER_ROLE / "defaults/main.yml").read_text(encoding="utf-8")
    )
    assert (
        _script_constant("DOCKER_KEY_FINGERPRINT")
        == (defaults["blitzecdn_docker_gpg_fingerprint"])
    )
    install = script_function("ensure_docker")
    # Checked before it is installed where apt reads it.
    assert install.index('docker_key_trusted "${key}"') < install.index(
        'install -o root -g root -m 0644 "${key}" "${DOCKER_KEYRING}"'
    )
    tasks = (DOCKER_ROLE / "tasks/main.yml").read_text(encoding="utf-8")
    assert tasks.index("Require Docker's own apt signing key") < tasks.index(
        "Configure the Docker apt repository"
    )


# --- the pin and the workflow that publishes it -------------------------------


def test_the_installer_and_the_role_pin_the_published_image_of_their_release():
    """`just release-version` moves both with the version; the tag publishes it.

    A downloaded install.sh has nothing else to read: its pin is the release.
    """
    defaults = yaml.safe_load(
        (CONTROL_PLANE_ROLE / "defaults/main.yml").read_text(encoding="utf-8")
    )
    assert defaults["blitzecdn_controlplane_image"] == _pin()
    result = _bash('printf "%s" "${CONTROL_PLANE_IMAGE}"')
    assert result.returncode == 0, result.stderr
    assert result.stdout == _pin()


def test_the_release_recipe_moves_every_pin_it_finds():
    justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    recipe = justfile[justfile.index("release-version version:") :]
    recipe = recipe[: recipe.index("\n# ---")]
    assert '"install.sh": (' in recipe
    script = SCRIPT.read_text(encoding="utf-8")
    pattern = re.search(r'"install\.sh": \(\n\s+r"(.+)",', recipe)
    assert pattern is not None
    assert len(re.findall(pattern.group(1), script)) == 1


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


def test_the_workflow_signs_what_the_installer_checks():
    """Signed keylessly by this workflow, by digest, with the same cosign line.

    The installer's signer is this workflow file at the release tag; a rename
    of the file or a move to another repository would leave every install
    refusing the image.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["image"]
    assert job["permissions"]["id-token"] == "write"
    steps = job["steps"]
    sign = next(
        step for step in steps if step.get("name") == "Sign the published image"
    )
    assert sign["run"] == 'cosign sign --yes "${IMAGE}@${DIGEST}"'
    assert sign["env"]["DIGEST"] == "${{ steps.build.outputs.digest }}"
    installer = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("sigstore/cosign-installer@")
    )
    workflow_cosign = installer["with"]["cosign-release"]
    script_cosign = re.search(r":(v\d+)\.", _script_constant("COSIGN_IMAGE"))
    assert script_cosign is not None
    assert workflow_cosign.split(".")[0] == script_cosign.group(1)
    assert _script_constant("CONTROL_PLANE_SIGNER") == (
        "https://github.com/misaf/blitze-cdn-cp/"
        f"{WORKFLOW.relative_to(REPO_ROOT)}@refs/tags/v"
    )
    attest = next(
        step for step in steps if step.get("name") == "Attest the release's installer"
    )
    assert attest["with"]["subject-path"] == "install.sh"
    assert attest["if"] == "startsWith(github.ref, 'refs/tags/')"


def test_the_checker_is_pinned_by_digest():
    assert re.fullmatch(
        r"ghcr\.io/sigstore/cosign/cosign:v[\d.]+@sha256:[0-9a-f]{64}",
        _script_constant("COSIGN_IMAGE"),
    )


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
        # Like the engine, a tag@digest reference finds the tagged image.
        '    grep -qxF -e "${image}" -e "${image%@*}" "${state}/present" || exit 1\n'
        '    if [[ "$*" == *RepoDigests* ]]; then\n'
        '      printf "%s\\n" "${REPO_DIGESTS:-}"\n'
        "    fi\n"
        # Every present image has the same ID unless IMAGE_ID says otherwise.
        '    if [[ "$*" == *.Id* ]]; then\n'
        '      printf "%s\\n" "sha256:${IMAGE_ID:-0123}"\n'
        "    fi\n"
        "    exit 0 ;;\n"
        "  pull*)\n"
        '    [[ -z "${PULL_FAILS:-}" ]] || exit 1\n'
        '    printf "%s\\n" "${image}" >> "${state}/present"; exit 0 ;;\n'
        "  load*)\n"
        '    printf "%s\\n" "${LOADED_IMAGE}" >> "${state}/present"; exit 0 ;;\n'
        "  build*)\n"
        "    exit 0 ;;\n"
        # cosign, checking a signature: VERIFY_FAILS makes it refuse.
        '  "run --rm")\n'
        '    [[ -z "${VERIFY_FAILS:-}" ]] || exit 1\n'
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
    return f"ghcr.io/misaf/blitzecdn-control-plane:{workspace_version()}"


def _script_constant(name: str) -> str:
    result = _bash(f'printf "%s" "${{{name}}}"')
    assert result.returncode == 0, result.stderr
    return result.stdout


def _verifications(engine: Path) -> list[str]:
    return [call for call in _calls(engine) if call.startswith("run --rm")]


def _cosign(reference: str) -> str:
    """The one signature check the installer may run for ``reference``."""
    return (
        f"run --rm --network=host {_script_constant('COSIGN_IMAGE')} verify"
        " --certificate-identity https://github.com/misaf/blitze-cdn-cp/"
        f".github/workflows/control-plane-image.yml@refs/tags/v{workspace_version()}"
        " --certificate-oidc-issuer https://token.actions.githubusercontent.com"
        f" {reference}"
    )


def test_the_pinned_image_is_pulled_and_run_by_its_registry_digest(engine: Path):
    digest = "sha256:" + "a" * 64
    result = _bash(
        f'obtain_control_plane_image "{_pin()}"',
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
    # Checked by the digest it is then run by, before anything runs it.
    assert _verifications(engine) == [
        _cosign(f"ghcr.io/misaf/blitzecdn-control-plane@{digest}")
    ]


def test_an_image_already_present_is_not_pulled_again_but_is_checked(engine: Path):
    """A re-run: the image is on the engine, and still has to be the signed one."""
    digest = "sha256:" + "e" * 64
    (engine / "present").write_text(f"{_pin()}\n", encoding="utf-8")

    result = _bash(
        f'obtain_control_plane_image "{_pin()}"',
        path=engine / "bin",
        env={"REPO_DIGESTS": f"ghcr.io/misaf/blitzecdn-control-plane@{digest}"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_pin()}@{digest}\n"
    assert not any(call.startswith("pull") for call in _calls(engine))
    assert _verifications(engine) == [
        _cosign(f"ghcr.io/misaf/blitzecdn-control-plane@{digest}")
    ]


def test_a_repair_does_not_recheck_the_image_the_services_run(
    engine: Path, tmp_path: Path
):
    """Installed from an archive, say: a re-run finds what is already running."""
    digest = "sha256:" + "2" * 64
    (engine / "present").write_text(f"{_pin()}\n", encoding="utf-8")
    compose = tmp_path / "control-plane.compose.yml"
    compose.write_text(f'  image: "{_pin()}"\n', encoding="utf-8")
    script = tmp_path / "install.sh"
    script.write_text(
        SCRIPT.read_text(encoding="utf-8").replace(
            "/etc/blitzecdn/control-plane.compose.yml", str(compose)
        ),
        encoding="utf-8",
    )

    result = _bash(
        f'obtain_control_plane_image "{_pin()}"',
        path=engine / "bin",
        env={"REPO_DIGESTS": f"ghcr.io/misaf/blitzecdn-control-plane@{digest}"},
        script=script,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_pin()}@{digest}\n"
    assert _verifications(engine) == []


def test_an_image_with_no_registry_digest_is_refused_unchecked(engine: Path):
    """Loaded or tagged by hand, it has no digest a signature could name."""
    (engine / "present").write_text(f"{_pin()}\n", encoding="utf-8")

    result = _bash(f'obtain_control_plane_image "{_pin()}"', path=engine / "bin")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "no registry digest" in result.stderr
    assert "BLITZECDN_CONTROL_PLANE_ARCHIVE" in result.stderr
    assert _verifications(engine) == []


def test_an_image_the_release_workflow_did_not_sign_is_never_run(engine: Path):
    digest = "sha256:" + "f" * 64

    result = _bash(
        f'obtain_control_plane_image "{_pin()}"',
        path=engine / "bin",
        env={
            "REPO_DIGESTS": f"ghcr.io/misaf/blitzecdn-control-plane@{digest}",
            "VERIFY_FAILS": "1",
        },
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert f"is not signed by the v{workspace_version()} release workflow" in (
        result.stderr
    )


def test_the_signature_check_can_be_waived_for_a_mirror(engine: Path):
    """Explicit, loud, and still run by the digest the registry served."""
    digest = "sha256:" + "1" * 64

    result = _bash(
        f'obtain_control_plane_image "{_pin()}"',
        path=engine / "bin",
        env={
            "REPO_DIGESTS": f"ghcr.io/misaf/blitzecdn-control-plane@{digest}",
            "BLITZECDN_CONTROL_PLANE_VERIFY": "0",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_pin()}@{digest}\n"
    assert "without checking its signature" in result.stderr
    assert _verifications(engine) == []


def test_an_image_named_by_digest_is_pulled_as_named(engine: Path):
    pinned = f"{_pin()}@sha256:{'c' * 64}"

    result = _bash(f'obtain_control_plane_image "{pinned}"', path=engine / "bin")

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{pinned}\n"
    assert f"pull {pinned}" in _calls(engine)
    assert _verifications(engine) == [
        _cosign(f"ghcr.io/misaf/blitzecdn-control-plane@sha256:{'c' * 64}")
    ]


def test_a_failed_pull_is_retried_and_then_names_the_fallback(engine: Path):
    result = _bash(
        f'obtain_control_plane_image "{_pin()}"',
        path=engine / "bin",
        env={"PULL_FAILS": "1"},
    )

    assert result.returncode == 1
    assert sum(call.startswith("pull") for call in _calls(engine)) == 3
    assert "BLITZECDN_CONTROL_PLANE_ARCHIVE" in result.stderr
    assert result.stdout == ""


def test_an_archive_is_loaded_instead_of_pulled(engine: Path, tmp_path: Path):
    archive = tmp_path / "control-plane.tar"
    archive.touch()

    result = _bash(
        f'obtain_control_plane_image "{_pin()}"',
        path=engine / "bin",
        env={"BLITZECDN_CONTROL_PLANE_ARCHIVE": str(archive), "LOADED_IMAGE": _pin()},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_pin()}\n"
    assert f"load --input {archive}" in _calls(engine)
    assert not any(call.startswith("pull") for call in _calls(engine))
    # The operator's own bytes, with no registry signature to look up.
    assert _verifications(engine) == []


def test_an_archive_without_the_expected_image_is_refused(engine: Path, tmp_path: Path):
    archive = tmp_path / "control-plane.tar"
    archive.touch()

    result = _bash(
        f'obtain_control_plane_image "{_pin()}"',
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
        f'obtain_control_plane_image "{_pin()}"',
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
    assert _verifications(engine) == []


def test_a_lone_installer_cannot_build_the_image_it_has_no_source_for(
    engine: Path, tmp_path: Path
):
    """A downloaded install.sh sits beside no Dockerfile; say so, build nothing."""
    lone = tmp_path / "download" / "install.sh"
    lone.parent.mkdir()
    lone.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    result = _bash(
        f'obtain_control_plane_image "{_pin()}"',
        path=engine / "bin",
        env={"BLITZECDN_CONTROL_PLANE_BUILD": "1"},
        script=lone,
    )

    assert result.returncode == 2
    assert "source checkout" in result.stderr
    assert not any(call.startswith("build") for call in _calls(engine))


# --- running Ansible from the image -------------------------------------------


def test_every_play_runs_in_the_image_against_the_host_through_nsenter():
    """The plays and their Ansible are the image's; the tasks run on the host.

    nsenter needs a privileged container in the host's PID namespace, and the
    plays name the connection themselves so nothing else can run them locally
    inside the container by mistake.
    """
    body = script_function("run_playbook")
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
