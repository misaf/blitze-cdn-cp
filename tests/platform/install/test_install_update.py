"""`update`: what it preserves, how it chooses, and how far it can be driven.

Sandboxed up to its point of no return, the Docker bootstrap before the new
image is pulled. Each test drives the run into a documented refusal or that
stop, and asserts what was, and was not, done to the host on the way.
"""

from __future__ import annotations

import re
from pathlib import Path

from support.install import (
    FAKE_IMAGE,
    FAKE_RELEASE,
    query,
    run_sandboxed,
    run_script,
    script_function,
    script_text,
    update_sandbox,
)
from support.paths import REPO_ROOT

# --- update: structural guarantees -------------------------------------------


def test_update_preserves_state_rather_than_removing_it():
    update = script_function("cmd_update")
    for destructive in ("converge_uninstall", "userdel", "rm -rf"):
        assert destructive not in update, f"update must never {destructive}"


def test_update_decides_where_it_is_going_before_it_changes_anything():
    """Every refusal about *which* release must cost no downtime."""
    update = script_function("cmd_update")
    selection = update.index(
        'run_image_helper "${installed}" blitzecdn.host.image_releases'
    )
    assert selection < update.index("backup create --only database")
    assert selection < update.index("ensure_docker")


def test_update_takes_a_backup_before_it_changes_anything():
    """A backup taken after the pull, or after the migration, protects nothing."""
    update = script_function("cmd_update")
    backup = update.index('"${CLI_WRAPPER}" "${backup_args[@]}"')
    assert backup < update.index("ensure_docker")
    assert backup < update.index("obtain_control_plane_image")


def test_a_release_move_leaves_stopping_the_services_to_the_role():
    """The role stops the services before it migrates, after the image is in place.

    `test_controlplane_initializes_schema_before_starting_services` holds that
    order; a stop here would only lengthen the downtime.
    """
    update = script_function("cmd_update")
    for direct in ("docker compose", "docker stop", "docker run"):
        assert direct not in update
    assert update.index("obtain_control_plane_image") < update.index(
        'converge_control_plane "${image}"'
    )


def test_the_installed_release_chooses_its_successor():
    """The host holds no code but its installer, so the choice runs in the image."""
    update = script_function("cmd_update")
    assert "installed_control_plane_image" in update
    assert 'run_image_helper "${installed}" blitzecdn.host.image_releases' in update
    helper = script_function("run_image_helper")
    assert '-I -m "${module}"' in helper


def test_update_pulls_the_target_from_the_repository_it_runs_from():
    """A host installed from a mirror keeps updating from that mirror."""
    assert query('image_repository "mirror.example:5000/cp:6.1.0@sha256:abc"') == (
        "mirror.example:5000/cp"
    )
    assert query('image_version "mirror.example:5000/cp:6.1.0@sha256:abc"') == "6.1.0"
    assert query('image_version "ghcr.io/misaf/blitzecdn-control-plane:6.1.0"') == (
        "6.1.0"
    )
    assert query('image_version "mirror.example:5000/cp" || echo none') == "none"


# --- update: sandboxed behaviour ---------------------------------------------


def _update(tmp_path: Path, *arguments: str, **env: str):
    script, stubs, root = update_sandbox(tmp_path)
    log = tmp_path / "order.log"
    selected = tmp_path / "select.log"
    result = run_sandboxed(
        script,
        "update",
        *arguments,
        input=env.pop("input", None),
        env_extra={
            "UPDATE_ORDER_LOG": str(log),
            "UPDATE_SELECT_LOG": str(selected),
            **env,
        },
        bin_dir=stubs,
    )
    order = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    selection = selected.read_text(encoding="utf-8") if selected.exists() else ""
    return result, order, selection, root


def test_update_refuses_a_host_with_no_installation(tmp_path: Path):
    script, stubs, root = update_sandbox(tmp_path)
    (root / "etc/blitzecdn/control-plane.compose.yml").unlink()

    result = run_sandboxed(script, "update", "--yes", bin_dir=stubs)

    assert result.returncode == 1
    assert "no installation found" in result.stderr


def test_update_refuses_when_the_installed_image_is_missing(tmp_path: Path):
    """It is the code that chooses the target, so without it nothing is chosen."""
    result, order, selection, _ = _update(tmp_path, "--yes", DOCKER_IMAGE_MISSING="1")

    assert result.returncode == 1
    assert f"{FAKE_IMAGE} is missing" in result.stderr
    assert "nothing was changed" in result.stderr
    assert selection == ""
    assert order == []


def test_update_moves_onto_the_release_the_installed_image_selects(tmp_path: Path):
    """The ordinary case. Driven to the Docker bootstrap, past the backup."""
    result, order, selection, _ = _update(tmp_path, "--yes", UPDATE_TARGET="3.1.0")

    assert result.returncode == 1
    assert f"--entrypoint python {FAKE_IMAGE} -I -m blitzecdn.host.image_releases" in (
        selection
    )
    assert selection.split()[-1] == FAKE_RELEASE, "asked about the installed release"
    assert "--major" not in selection
    assert "to    3.1.0" in result.stderr
    assert order == ["backup", "docker bootstrap"]


def test_a_refused_selection_changes_nothing(tmp_path: Path):
    result, order, _, _ = _update(
        tmp_path, "--yes", UPDATE_SELECT_ERROR="no 3.x release is published"
    )

    assert result.returncode == 1
    assert "no 3.x release is published; nothing was changed" in result.stderr
    assert order == [], "a host that could not be updated was still touched"


def test_update_is_a_no_op_when_already_on_the_newest_release(tmp_path: Path):
    result, order, _, _ = _update(tmp_path, "--yes", UPDATE_TARGET=FAKE_RELEASE)

    assert result.returncode == 0
    assert f"Already on {FAKE_RELEASE}" in result.stdout
    assert "nothing to update" in result.stdout
    assert order == [], "an up-to-date host was still touched"


def test_an_operator_can_name_the_target_image(tmp_path: Path):
    """For a mirror: the named image is the target, checked by the installed code."""
    named = "mirror.example/blitzecdn-control-plane:3.2.0"
    result, _, selection, _ = _update(
        tmp_path,
        input="n\n",
        BLITZECDN_CONTROL_PLANE_IMAGE=named,
        UPDATE_TARGET="3.2.0",
    )

    assert result.returncode == 0
    assert selection.split()[-3:] == [FAKE_RELEASE, "--target", "3.2.0"]
    assert "to    3.2.0" in result.stderr


def test_a_named_image_must_carry_its_release_version(tmp_path: Path):
    result, order, selection, _ = _update(
        tmp_path,
        "--yes",
        BLITZECDN_CONTROL_PLANE_IMAGE="mirror.example/cp@sha256:" + "a" * 64,
    )

    assert result.returncode == 2
    assert "tagged with its release version" in result.stderr
    assert selection == ""
    assert order == []


def test_update_refuses_to_build_from_source(tmp_path: Path):
    """A source build has no release version to move from or to."""
    result, order, _, _ = _update(tmp_path, "--yes", BLITZECDN_CONTROL_PLANE_BUILD="1")

    assert result.returncode == 2
    assert "is for standalone" in result.stderr
    assert order == []


def test_update_asks_for_confirmation_and_can_be_cancelled(tmp_path: Path):
    result, order, _, _ = _update(tmp_path, input="n\n")

    assert result.returncode == 0
    assert "Cancelled" in result.stdout
    assert f"from  {FAKE_RELEASE}" in result.stderr, "the release it leaves"
    assert "to    3.1.0" in result.stderr, "and the one it moves to"
    assert order == []


def test_update_stops_nothing_when_the_backup_fails(tmp_path: Path):
    """A failed backup must abort while the controller is still serving."""
    result, order, _, _ = _update(tmp_path, "--yes", UPDATE_BACKUP_STATUS="1")

    assert result.returncode == 1
    assert "backup failed" in result.stderr
    assert order == ["backup"]


def test_update_backs_up_only_the_database(tmp_path: Path):
    backup_args = tmp_path / "backup-args.log"
    _update(tmp_path, "--yes", UPDATE_BACKUP_ARGS_LOG=str(backup_args))

    assert backup_args.read_text(encoding="utf-8").strip() == (
        "backup create --only database"
    )


def test_update_can_skip_the_backup_but_says_so(tmp_path: Path):
    result, order, _, _ = _update(tmp_path, "--yes", "--no-backup")

    assert "Skipping the database backup" in result.stdout
    assert order == ["docker bootstrap"], "no backup was taken"


def test_update_refuses_when_backup_wrapper_is_missing(tmp_path: Path):
    """Without the wrapper there is no backup, and that is said, not skipped."""
    script, stubs, root = update_sandbox(tmp_path)
    (root / "usr/local/bin/blitzecdn").unlink()
    log = tmp_path / "order.log"

    result = run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={"UPDATE_ORDER_LOG": str(log)},
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "the database backup cannot be taken; nothing was changed" in result.stderr
    assert "--no-backup" in result.stderr
    assert not log.exists()


def test_every_command_uses_the_shared_option_parser():
    """One parser, and one table that routes every command through it.

    Asserted against the sourced script: the table must have a row per command.
    """
    assert script_text().count("while [[ $# -gt 0 ]]") == 1
    for command in ("standalone", "uninstall", "update"):
        assert query(f"command_field {command} 2") != "", (
            f"{command} has no row in COMMAND_TABLE"
        )


def test_the_control_plane_image_installs_the_single_application():
    dockerfile = (
        REPO_ROOT / "src/blitzecdn/assets/docker/control-plane/Dockerfile"
    ).read_text()
    code = "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )
    assert "uv sync --frozen --no-dev --no-editable" in code
    assert "--extra" not in code
    assert "--all-packages" not in code


def test_the_production_recipe_installs_the_single_application():
    justfile = (REPO_ROOT / "justfile").read_text()
    recipe = re.search(r"\ninstall-prod:\n(.*?)(?=\n\S)", justfile, re.DOTALL)
    assert recipe
    assert "uv sync --frozen --no-dev" in recipe.group(1)
    assert "--extra" not in recipe.group(1)


def test_the_command_table_and_the_help_agree_on_every_option():
    """The table is what accepts an option; the help is what advertises it."""
    # The two whole-host operations are spelled as flags on the command line,
    # so the row name and the token an operator types are not the same word.
    invocations = {
        "standalone": "standalone",
        "update": "update",
        "uninstall": "--uninstall",
    }
    for command, token in invocations.items():
        declared = query(f"command_field {command} 4").split()
        assert declared, f"{command} declares no options"
        helped = run_script(token, "--help").stdout
        for option in declared:
            assert option in helped, f"{command} accepts {option} but never says so"
