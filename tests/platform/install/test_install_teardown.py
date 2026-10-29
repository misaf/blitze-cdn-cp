"""`--uninstall` and `--fresh`: dispatch, guarantees, and sandboxed runs.

The destructive paths, driven for real inside the sandbox: what is removed,
what is left alone, and that a rebuild takes the same path as a brand-new
server.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml
from support.install import (
    FAKE_IMAGE,
    fake_installation,
    fake_unrelated,
    instrument,
    query,
    run_sandboxed,
    run_script,
    script_function,
    script_section,
    script_text,
    stub_bin,
)
from support.paths import CORE_ANSIBLE, REPO_ROOT

# --- fresh rebuild and uninstall: dispatch ------------------------------------


def test_dispatch_recognizes_the_destructive_subcommands():
    script = script_text()
    assert re.search(r"\(--uninstall|--uninstall\)|cmd_uninstall", script)
    assert re.search(r"\(--fresh|--fresh\)|cmd_fresh", script)


@pytest.mark.parametrize("subcommand", ["--uninstall", "--fresh"])
def test_destructive_subcommands_refuse_to_run_unprivileged(subcommand):
    result = run_script(subcommand)
    assert result.returncode == 1
    assert "sudo" in result.stderr


@pytest.mark.parametrize(
    ("subcommand", "help_fragment"),
    [
        ("--uninstall", "--yes"),
        ("--fresh", "--admin-cidr"),
    ],
)
def test_destructive_subcommand_help_does_not_require_root(subcommand, help_fragment):
    result = run_script(subcommand, "--help")
    assert result.returncode == 0
    assert help_fragment in result.stdout


def test_uninstall_rejects_unknown_options():
    result = run_script("--uninstall", "--not-an-option")
    assert result.returncode == 2
    assert "unknown option" in result.stderr


# --- fresh rebuild and uninstall: structural guarantees ----------------------


def test_destructive_commands_survive_deleting_their_own_directory():
    """Both remove $INSTALL_DIR, so both must continue from a copied script."""
    for name in ("uninstall", "fresh"):
        section = script_section(name)
        assert "reexec_from_private_copy BLITZECDN_UNINSTALL_REEXEC" in section
        # The copy must be taken before final self-removal deletes this file.
        assert section.index("reexec_from_private_copy") < section.index(
            "remove_installation_directory"
        )


def test_bash_delegates_all_system_teardown_to_ansible():
    uninstall = script_section("uninstall")
    assert "converge_uninstall" in uninstall
    assert uninstall.index("converge_uninstall") < uninstall.index(
        "remove_installation_directory"
    )
    for operation in ("systemctl", "userdel", "loginctl", "pkill", "nginx -t"):
        assert operation not in uninstall
    assert script_function("remove_installation_directory").strip() == (
        'rm -rf -- "${INSTALL_DIR}"'
    )


def test_uninstall_reuses_the_canonical_edge_teardown_role():
    uninstall_tasks = yaml.safe_load(
        (CORE_ANSIBLE / "roles/blitzecdn_uninstall/tasks/main.yml").read_text(
            encoding="utf-8"
        )
    )
    handoffs = [
        task
        for task in uninstall_tasks
        if task.get("ansible.builtin.include_role", {}).get("name")
        == "blitzecdn_edge_teardown"
    ]
    assert len(handoffs) == 1
    assert handoffs[0]["vars"]["blitzecdn_edge_teardown_remove_logs"] is True

    serialized = yaml.safe_dump(uninstall_tasks)
    for duplicated_edge_detail in (
        "nginx_marker",
        "sites-available",
        "sites-enabled",
        "blitzecdn-managed-sites",
        "/var/cache/nginx",
        "/var/log/nginx",
    ):
        assert duplicated_edge_detail not in serialized


def test_destructive_commands_require_confirmation():
    for name in ("uninstall", "fresh"):
        section = script_section(name)
        assert "confirm_destructive" in section
        assert 'confirm_destructive "${parsed_yes}"' in section
    parser = script_function("parse_options")
    # The flag is handled once, in the shared parser. Matched loosely, since the
    # case label grows with every new flag.
    assert re.search(r"^      --yes\|.*\)$", parser, re.MULTILINE)
    # And it is the parser that records it, for every command that takes it.
    for command in ("uninstall", "fresh", "update"):
        usage = query(f"command_field {command} 2")
        assert (
            query(f'parse_options {command} {usage} --yes\necho "${{parsed_yes}}"')
            == "1"
        )
        assert query(f'parse_options {command} {usage}\necho "${{parsed_yes}}"') == "0"
    confirm = script_function("confirm_destructive")
    assert "read -r -p" in confirm
    assert "Cancelled." in confirm


def test_fresh_reuses_the_same_ansible_teardown_as_uninstall():
    fresh = script_section("fresh")
    assert "converge_uninstall" in fresh
    assert "remove_installation_directory" in fresh
    assert 'run "sudo ./install.sh --uninstall"' not in fresh


def _release_branch() -> str:
    """The branch a release installation tracks, from the workspace version.

    `install.sh` preserves the current major's line across a rebuild; derived so
    these tests do not break when the version moves.
    """
    version = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return f"{re.search(r'^version = \"(\d+)', version, re.M).group(1)}.x"


def test_fresh_preserves_the_running_source_line_like_a_new_server():
    """Which revision is `install_release.fresh_source`'s; this is how it is used."""
    fresh = script_section("fresh")
    assert fresh.index("release_plan source") < fresh.index("converge_uninstall")
    assert 'git clone --branch "${revision}"' in fresh
    assert "git clone --depth 1" not in fresh
    assert 'git -C "${staging}" checkout --detach "${revision}"' in fresh
    assert '"${INSTALL_DIR}/install.sh" standalone ' in fresh
    assert '${parsed_forward_args[@]+"${parsed_forward_args[@]}"}' in fresh


def test_fresh_refuses_to_rebuild_without_a_source_checkout():
    fresh = script_section("fresh")
    assert "${INSTALL_DIR} is not a Git checkout" in fresh


# --- fresh rebuild and uninstall: sandboxed behaviour ------------------------


def test_uninstall_removes_owned_artifacts_and_leaves_unrelated(tmp_path):
    sandbox = tmp_path / "sandbox"
    script, root = instrument(sandbox)
    stub_bin(sandbox, root)
    owned = fake_installation(root)
    unrelated = fake_unrelated(root)

    result = run_sandboxed(script, "--uninstall", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    for path in owned:
        assert not path.exists(), f"owned artifact survived uninstall: {path}"
    for path in unrelated:
        assert path.exists(), f"unrelated file was removed: {path}"


def test_uninstall_asks_for_confirmation_and_can_be_cancelled(tmp_path):
    sandbox = tmp_path / "sandbox"
    script, root = instrument(sandbox)
    stub_bin(sandbox, root)
    owned = fake_installation(root)

    cancelled = run_sandboxed(script, "--uninstall", input="n\n")
    assert cancelled.returncode == 0
    assert "Cancelled" in cancelled.stdout
    assert all(path.exists() for path in owned)


def test_uninstall_requires_the_installed_image(tmp_path):
    """The teardown is the image's Ansible; without it nothing is touched."""
    sandbox = tmp_path / "sandbox"
    script, root = instrument(sandbox)
    stub_bin(sandbox, root)
    owned = fake_installation(root)

    result = run_sandboxed(
        script, "--uninstall", "--yes", env_extra={"DOCKER_IMAGE_MISSING": "1"}
    )

    assert result.returncode == 1
    assert f"the control-plane image {FAKE_IMAGE} is missing" in result.stderr
    assert all(path.exists() for path in owned)


def test_uninstall_requires_the_record_of_which_image_is_installed(tmp_path):
    sandbox = tmp_path / "sandbox"
    script, root = instrument(sandbox)
    stub_bin(sandbox, root)
    owned = fake_installation(root)

    (root / "etc/blitzecdn/control-plane.compose.yml").unlink()
    result = run_sandboxed(script, "--uninstall", "--yes")

    assert result.returncode == 1
    assert "no control-plane image is recorded" in result.stderr
    assert (root / "opt/blitzecdn/install.sh").exists()
    assert any(path.exists() for path in owned)


def test_uninstall_finishes_from_the_image_when_the_checkout_is_already_gone(
    tmp_path: Path,
):
    """The play ships in the image, so a deleted checkout no longer strands a host."""
    sandbox = tmp_path / "sandbox"
    script, root = instrument(sandbox)
    stub_bin(sandbox, root)
    owned = fake_installation(root)

    shutil.rmtree(root / "opt/blitzecdn")

    result = run_sandboxed(script, "--uninstall", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(path.exists() for path in owned)


def test_uninstall_removes_the_image_only_after_the_play_that_runs_in_it(
    tmp_path: Path,
):
    sandbox = tmp_path / "sandbox"
    script, root = instrument(sandbox)
    stub_bin(sandbox, root)
    fake_installation(root)
    log = tmp_path / "docker.log"

    result = run_sandboxed(
        script, "--uninstall", "--yes", env_extra={"DOCKER_LOG": str(log)}
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    play = next(i for i, call in enumerate(calls) if "uninstall.yml" in call)
    assert FAKE_IMAGE in calls[play]
    removal = next(
        i for i, call in enumerate(calls) if call.startswith("docker image rm")
    )
    assert play < removal
    # By ID: the stub answers every inspection with this one.
    assert calls[removal] == "docker image rm --force sha256:0123"


def test_fresh_rebuild_removes_then_reinstalls_like_a_brand_new_server(
    tmp_path: Path,
):
    sandbox = tmp_path / "sandbox"
    script, root = instrument(sandbox)
    stub_bin(sandbox, root)
    marker = tmp_path / "fresh-reinstalled"
    fake_installation(root)

    result = run_sandboxed(
        script,
        "--fresh",
        "--yes",
        env_extra={"FRESH_REINSTALL_MARKER": str(marker)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists(), "the fresh install.sh never ran"
    assert marker.read_text().strip() == "fresh-reinstalled"
    assert (root / "opt/blitzecdn/install.sh").exists(), "no fresh checkout was created"


def test_fresh_rebuild_keeps_a_release_branch_checkout_on_its_branch(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    script, root = instrument(sandbox)
    stub_bin(sandbox, root)
    reinstall_marker = tmp_path / "fresh-reinstalled"
    clone_marker = tmp_path / "git-clone-arguments"
    fake_installation(root)

    result = run_sandboxed(
        script,
        "--fresh",
        "--yes",
        env_extra={
            "FRESH_GIT_TAG": "",
            "FRESH_GIT_BRANCH": _release_branch(),
            "FRESH_GIT_CLONE_MARKER": str(clone_marker),
            "FRESH_REINSTALL_MARKER": str(reinstall_marker),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert reinstall_marker.exists()
    clone_args = clone_marker.read_text(encoding="utf-8")
    assert f"clone --branch {_release_branch()}" in clone_args
    assert "--depth" not in clone_args


def test_fresh_refuses_to_rebuild_without_a_git_checkout(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    script, root = instrument(sandbox)
    stub_bin(sandbox, root)
    fake_installation(root, with_git=False)

    result = run_sandboxed(script, "--fresh", "--yes")

    assert result.returncode == 1
    assert "is not a Git checkout" in result.stderr


def test_fresh_clone_failure_preserves_the_running_installation(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    script, root = instrument(sandbox)
    stub_bin(sandbox, root)
    owned = fake_installation(root)

    result = run_sandboxed(
        script,
        "--fresh",
        "--yes",
        env_extra={"FRESH_GIT_CLONE_FAIL": "1"},
    )

    assert result.returncode == 1
    assert "current installation was not changed" in result.stderr
    assert all(path.exists() for path in owned)


def test_fresh_refuses_an_unsupported_platform_before_destroying_anything(
    tmp_path: Path,
):
    """The refusal that matters most, because the alternative is unrecoverable.

    `--fresh` uninstalls then reruns `standalone`; on a host the reinstall would
    refuse, that is a deletion. So the platform is read before the confirmation.
    """
    sandbox = tmp_path / "sandbox"
    script, root = instrument(sandbox)
    stub_bin(sandbox, root)
    owned = fake_installation(root)
    (root / "etc/os-release").write_text(
        'ID=debian\nVERSION_ID="13"\n', encoding="utf-8"
    )

    result = run_sandboxed(script, "--fresh", "--yes")

    assert result.returncode == 1
    assert "requires Ubuntu 26.04" in result.stderr
    assert all(path.exists() for path in owned)
