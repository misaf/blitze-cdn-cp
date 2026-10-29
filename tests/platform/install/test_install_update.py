"""`update`: what it preserves, and how far it can be driven.

Sandboxed up to its point of no return, the virtualenv rebuild. Each test
drives the run into a documented refusal and asserts the host was left
serving.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from support.install import (
    _function,
    _query,
    _run,
    _run_sandboxed,
    _script,
    _section,
    _update_sandbox,
)
from support.paths import REPO_ROOT

# --- update: structural guarantees -------------------------------------------


def test_update_preserves_state_rather_than_removing_it():
    """The whole point of the command: --fresh destroys, update does not."""
    update = _function("move_release")
    for destructive in (
        "converge_uninstall",
        "remove_installation_directory",
        "reexec_from_private_copy",
        "userdel",
    ):
        assert destructive not in update, f"update must never {destructive}"


def test_update_takes_a_backup_before_it_changes_anything():
    update = _function("move_release")
    assert "backup create --only database" in update
    # Ordering is the guarantee. A backup taken after the checkout, or after the
    # migration, is a backup of the thing it was supposed to protect against.
    marker = "backup create --only database"
    assert update.index(marker) < update.index('repo_git checkout --quiet "${target}"')
    assert update.index(marker) < update.index("ensure_docker")


@pytest.mark.parametrize("subcommand", ["update", "upgrade"])
def test_a_release_move_leaves_stopping_the_services_to_the_role(subcommand: str):
    """The role stops the services before it migrates, after the image is built.

    `test_controlplane_initializes_schema_before_starting_services` holds that
    order; a stop here would only lengthen the downtime.
    """
    assert f"move_release {subcommand}" in _section(subcommand)
    section = _function("move_release")
    for direct in ("docker compose", "docker stop", "docker run"):
        assert direct not in section
    assert section.index("ensure_docker") < section.index("converge_control_plane")


def test_a_release_move_runs_the_image_its_target_pins():
    """The image is chosen after the checkout, from the checkout.

    Read before it, the pin would be the release being left, and the update
    would recreate the services on the image they already run.
    """
    section = _function("move_release")
    checkout = section.index('repo_git checkout --quiet "${target}"')
    assert checkout < section.index("obtain_control_plane_image")
    assert section.index("obtain_control_plane_image") < section.index(
        'converge_control_plane "${image}"'
    )
    pin = _function("pinned_control_plane_image")
    assert '"${script_dir}/${CONTROL_PLANE_IMAGE_PIN}"' in pin


def test_update_only_ever_moves_forward_onto_a_release_tag():
    """A merge or a rewrite on a server is never what an operator meant."""
    update = _function("move_release")
    assert 'release_plan "${mode}"' in update
    assert 'repo_git checkout --quiet "${target}"' in update
    # `merge --ff-only`, not a bare "merge": `merge-base` contains that word.
    forbidden = ("git pull", "merge --ff-only", "--force", "reset --hard", "rebase")
    for operation in forbidden:
        assert operation not in update


def test_update_decides_where_it_is_going_before_it_changes_anything():
    """Every refusal about *which* release must cost no downtime."""
    update = _function("move_release")
    assert update.index('release_plan "${mode}"') < update.index(
        "backup create --only database"
    )


def test_update_verifies_the_code_it_is_about_to_run_as_root():
    """The origin and clean-tree checks are `install_release.prepare`'s, which
    `release_plan` runs before anything is fetched or changed."""
    update = _function("move_release")
    assert update.index("${INSTALL_DIR} is not a Git checkout") < update.index(
        'release_plan "${mode}"'
    )


# --- update: sandboxed behaviour ---------------------------------------------


def test_update_refuses_a_checkout_with_local_modifications(tmp_path: Path):
    script, stubs, _ = _update_sandbox(tmp_path)

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={"UPDATE_GIT_DIRTY": " M install.sh\n"},
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "local modifications" in result.stderr


def test_update_refuses_without_a_git_checkout(tmp_path: Path):
    script, stubs, _ = _update_sandbox(tmp_path, with_git=False)

    result = _run_sandboxed(script, "update", "--yes", bin_dir=stubs)

    assert result.returncode == 1
    assert "is not a Git checkout" in result.stderr


def test_update_leaves_the_host_alone_when_the_fetch_fails(tmp_path: Path):
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={"UPDATE_GIT_FETCH_STATUS": "1", "UPDATE_ORDER_LOG": str(log)},
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "could not fetch" in result.stderr
    assert not log.exists(), "a failed fetch stopped no services and took no backup"


def test_update_moves_a_detached_release_onto_the_newest_tag_in_its_line(
    tmp_path: Path,
):
    """The ordinary case: a release install follows its line without being told.

    Driven into the checkout failure so the run stops before the rebuild.
    """
    script, stubs, _ = _update_sandbox(tmp_path)

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={
            "UPDATE_GIT_EXACT_TAG": "v3.0.0",
            "UPDATE_GIT_TAGS": "v3.1.0\nv3.0.0\n",
            "UPDATE_GIT_CHECKOUT_FAILS": "1",
        },
        bin_dir=stubs,
    )

    assert "Checking out v3.1.0" in result.stdout
    assert result.returncode == 1
    assert "the services were not stopped" in result.stderr, (
        "a failed checkout must say the host is still serving"
    )
    assert "checkout v3.0.0" in result.stderr, "and name the release to restore"


def test_update_ignores_releases_from_another_major_line(tmp_path: Path):
    """A 4.0.0 release is a migration an operator opts into, not an update."""
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={
            "UPDATE_PROJECT_VERSION": "3.0.0",
            # What `tag --list "v3.*"` would return: the v4 line is not offered
            # to a v3 host at all, so an empty answer is a v3-only repository.
            "UPDATE_GIT_TAGS": "",
            "UPDATE_ORDER_LOG": str(log),
        },
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "no v3.x release tag" in result.stderr
    assert "nothing was changed" in result.stderr
    assert not log.exists()


def test_update_refuses_a_checkout_that_is_not_an_ancestor_of_the_release(
    tmp_path: Path,
):
    """Local commits, or a host already past the tag: --fresh is the tool."""
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={
            "UPDATE_GIT_ANCESTOR_STATUS": "1",
            "UPDATE_ORDER_LOG": str(log),
        },
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "is not an ancestor of v3.1.0" in result.stderr
    assert "--fresh" in result.stderr
    assert "nothing was changed" in result.stderr
    assert not log.exists(), "a host that could not be updated was still taken down"


def test_update_refuses_when_the_project_version_is_unreadable(tmp_path: Path):
    """Without a version there is no release line, so there is no target."""
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={"UPDATE_PROJECT_VERSION": "", "UPDATE_ORDER_LOG": str(log)},
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "nothing was changed" in result.stderr
    assert not log.exists()


def test_update_is_a_no_op_when_already_on_the_newest_release(tmp_path: Path):
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={
            "UPDATE_GIT_EXACT_TAG": "v3.1.0",
            "UPDATE_GIT_TAGS": "v3.1.0\n",
            "UPDATE_ORDER_LOG": str(log),
        },
        bin_dir=stubs,
    )

    assert result.returncode == 0
    assert "Already on v3.1.0" in result.stdout
    assert "nothing to update" in result.stdout
    assert not log.exists(), "an up-to-date host was still taken down"


def test_update_asks_for_confirmation_and_can_be_cancelled(tmp_path: Path):
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        input="n\n",
        env_extra={"UPDATE_ORDER_LOG": str(log)},
        bin_dir=stubs,
    )

    assert result.returncode == 0
    assert "Cancelled" in result.stdout
    assert "v3.0.0" in result.stderr, "the prompt must name the release it leaves"
    assert "v3.1.0" in result.stderr, "and the one it moves to"
    assert "(12 commits)" in result.stderr, "and how far apart they are"
    assert "-g" not in result.stderr, (
        "an operator is shown release versions, never a describe suffix"
    )
    assert not log.exists()


def test_update_names_the_drifted_release_a_branch_checkout_is_leaving(
    tmp_path: Path,
):
    """A host past its tag says so, rather than claiming to be on that release."""
    script, stubs, _ = _update_sandbox(tmp_path)

    result = _run_sandboxed(
        script,
        "update",
        input="n\n",
        env_extra={
            "UPDATE_GIT_EXACT_TAG": "",
            "UPDATE_GIT_NEAREST_TAG": "v3.0.0",
            "UPDATE_GIT_DRIFT": "4",
        },
        bin_dir=stubs,
    )

    assert "v3.0.0 (+4 commits)" in result.stderr


def test_update_falls_back_to_the_recorded_version_when_no_tag_is_reachable(
    tmp_path: Path,
):
    script, stubs, _ = _update_sandbox(tmp_path)

    result = _run_sandboxed(
        script,
        "update",
        input="n\n",
        env_extra={
            "UPDATE_GIT_EXACT_TAG": "",
            "UPDATE_GIT_NEAREST_TAG": "",
            "UPDATE_GIT_SHORT_SHA": "abc1234",
        },
        bin_dir=stubs,
    )

    assert "v3.0.0 (abc1234)" in result.stderr


def test_update_stops_nothing_when_the_backup_fails(tmp_path: Path):
    """A failed backup must abort while the controller is still serving."""
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={"UPDATE_BACKUP_STATUS": "1", "UPDATE_ORDER_LOG": str(log)},
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "backup failed" in result.stderr
    assert log.read_text(encoding="utf-8").split() == ["backup"]


def test_update_backs_up_and_stops_nothing_itself(tmp_path: Path):
    """Driven into the checkout failure so it stops before the heavy rebuild."""
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={"UPDATE_GIT_CHECKOUT_FAILS": "1", "UPDATE_ORDER_LOG": str(log)},
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert log.read_text(encoding="utf-8").splitlines() == ["backup"]


def test_update_can_skip_the_backup_but_says_so(tmp_path: Path):
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        "--no-backup",
        env_extra={"UPDATE_GIT_CHECKOUT_FAILS": "1", "UPDATE_ORDER_LOG": str(log)},
        bin_dir=stubs,
    )

    assert "Skipping the database backup" in result.stdout
    assert not log.exists(), "no backup was taken and nothing was stopped"


def test_every_command_uses_the_shared_option_parser():
    """One parser, and one table that routes every command through it.

    Asserted against the sourced script: the table must have a row per command.
    """
    assert _script().count("while [[ $# -gt 0 ]]") == 1
    for command in ("standalone", "uninstall", "fresh", "update", "upgrade"):
        assert _query(f"command_field {command} 2") != "", (
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
        "fresh": "--fresh",
    }
    for command, token in invocations.items():
        declared = _query(f"command_field {command} 4").split()
        assert declared, f"{command} declares no options"
        helped = _run(token, "--help").stdout
        for option in declared:
            assert option in helped, f"{command} accepts {option} but never says so"


def test_private_copy_helper_copies_once_and_cleans_up_after_itself():
    """The mechanics the three destructive paths used to repeat verbatim."""
    helper = _function("reexec_from_private_copy")
    assert 'install -m 0700 -- "$0" "${copy}"' in helper
    assert 'exec env "${guard}=1" "${copy}" "${original_args[@]}"' in helper
    # The copy deletes itself through the shared cleanup stack; its own EXIT trap
    # would replace the handler and strand the rest.
    assert 'cleanup_paths+=("$0")' in helper
    code = [line for line in helper.splitlines() if not line.lstrip().startswith("#")]
    assert not any(line.lstrip().startswith("trap ") for line in code), (
        "a second EXIT trap here replaces the shared cleanup handler"
    )
