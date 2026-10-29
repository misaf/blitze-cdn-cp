"""`--uninstall`: dispatch, guarantees, and sandboxed runs.

The destructive path, driven for real inside the sandbox: what is removed and
what is left alone.
"""

from __future__ import annotations

import re
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
    stub_bin,
)
from support.paths import CORE_ANSIBLE

# --- uninstall: dispatch -------------------------------------------------------


def test_uninstall_refuses_to_run_unprivileged():
    result = run_script("--uninstall")
    assert result.returncode == 1
    assert "sudo" in result.stderr


def test_uninstall_help_does_not_require_root():
    result = run_script("--uninstall", "--help")
    assert result.returncode == 0
    assert "--yes" in result.stdout
    # The one thing it keeps, said where the operator decides.
    assert "/var/backups/blitzecdn" in result.stdout


@pytest.mark.parametrize("removed", ["--fresh", "upgrade"])
def test_removed_lifecycle_commands_are_unknown(removed: str):
    """Reinstalling is uninstall then standalone; crossing a major is update --major."""
    result = run_script(removed)
    assert result.returncode == 2


def test_uninstall_rejects_unknown_options():
    result = run_script("--uninstall", "--not-an-option")
    assert result.returncode == 2
    assert "unknown option" in result.stderr


# --- uninstall: structural guarantees ------------------------------------------


def test_bash_delegates_all_system_teardown_to_ansible():
    """Bash removes only the image the play ran in; there is no tree to delete."""
    uninstall = script_section("uninstall")
    assert "converge_uninstall" in uninstall
    for operation in ("systemctl", "userdel", "loginctl", "pkill", "nginx -t", "rm -"):
        assert operation not in uninstall
    teardown = script_function("converge_uninstall")
    assert teardown.index("uninstall.yml") < teardown.index("docker image rm")


def test_the_uninstall_play_removes_the_installer_it_was_run_by():
    tasks = (CORE_ANSIBLE / "roles/blitzecdn_uninstall/tasks/main.yml").read_text(
        encoding="utf-8"
    )
    defaults = yaml.safe_load(
        (CORE_ANSIBLE / "roles/blitzecdn_uninstall/defaults/main.yml").read_text(
            encoding="utf-8"
        )
    )
    assert "{{ blitzecdn_uninstall_installer }}" in tasks
    assert defaults["blitzecdn_uninstall_installer"] == (
        "/usr/local/sbin/blitzecdn-install"
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
    section = script_section("uninstall")
    assert 'confirm_destructive "${parsed_yes}"' in section
    parser = script_function("parse_options")
    # The flag is handled once, in the shared parser. Matched loosely, since the
    # case label grows with every new flag.
    assert re.search(r"^      --yes\|.*\)$", parser, re.MULTILINE)
    # And it is the parser that records it, for every command that takes it.
    for command in ("uninstall", "update"):
        usage = query(f"command_field {command} 2")
        assert (
            query(f'parse_options {command} {usage} --yes\necho "${{parsed_yes}}"')
            == "1"
        )
        assert query(f'parse_options {command} {usage}\necho "${{parsed_yes}}"') == "0"
    confirm = script_function("confirm_destructive")
    assert "read -r -p" in confirm
    assert "Cancelled." in confirm


# --- uninstall: sandboxed behaviour -------------------------------------------


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
    assert all(
        path.exists() for path in owned if path.name != "control-plane.compose.yml"
    )


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
