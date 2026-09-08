"""`update`: what it preserves, and how far it can be driven.

Sandboxed only as far as its point of no return — past that it rebuilds the
virtualenv, which downloads uv and resolves a lockfile. Each test here drives
the run into a documented refusal and asserts the host was left serving.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from install_support import (
    _function,
    _query,
    _run,
    _run_sandboxed,
    _script,
    _section,
    _update_sandbox,
)
from paths import CORE_ANSIBLE, REPO_ROOT

# --- update: structural guarantees -------------------------------------------


def test_update_preserves_state_rather_than_removing_it():
    """The whole point of the command: --fresh destroys, update does not."""
    update = _section("update")
    for destructive in (
        "converge_uninstall",
        "remove_installation_directory",
        "reexec_from_private_copy",
        "userdel",
    ):
        assert destructive not in update, f"update must never {destructive}"


def test_update_takes_a_backup_before_it_changes_anything():
    update = _section("update")
    assert "backup create --only database" in update
    # Ordering is the guarantee. A backup taken after the checkout, or after the
    # migration, is a backup of the thing it was supposed to protect against.
    marker = "backup create --only database"
    assert update.index(marker) < update.index("stop_control_plane_services")
    assert update.index(marker) < update.index("bootstrap_runtime")


def test_update_stops_the_services_before_migrating_the_schema():
    """The converge runs `blitzecdn setup --schema-only` against a live host."""
    update = _section("update")
    assert update.index("stop_control_plane_services") < update.index(
        "converge_control_plane"
    )
    assert update.index("bootstrap_runtime") < update.index("converge_control_plane")


def test_update_only_ever_moves_forward_onto_a_release_tag():
    """A merge or a rewrite on a server is never what an operator meant.

    The forward-only question is now asked directly, rather than inferred from
    a fast-forward merge failing, so the assertion names the check itself.
    """
    update = _section("update")
    assert 'repo_git merge-base --is-ancestor HEAD "${target}^{commit}"' in update
    assert 'repo_git checkout --quiet "${target}"' in update
    # `merge --ff-only`, not a bare "merge": `merge-base` contains that word.
    forbidden = ("git pull", "merge --ff-only", "--force", "reset --hard", "rebase")
    for operation in forbidden:
        assert operation not in update


def test_update_never_crosses_a_major_line():
    """The one property that keeps an unattended updater from a major upgrade."""
    resolve = _function("latest_release_tag")
    assert 'repo_git tag --list "v${major}.*" --sort=-v:refname' in resolve
    # A pre-release or a branch-shaped tag is not an installable release.
    assert "^v[0-9]+\\.[0-9]+\\.[0-9]+$" in resolve
    assert 'latest_release_tag "${major}"' in _section("update")


def test_update_decides_where_it_is_going_before_it_stops_anything():
    """Every refusal about *which* release must cost no downtime."""
    update = _section("update")
    assert update.index("latest_release_tag") < update.index(
        "backup create --only database"
    )
    assert update.index("merge-base --is-ancestor") < update.index(
        "stop_control_plane_services"
    )


def test_update_verifies_the_code_it_is_about_to_run_as_root():
    update = _section("update")
    assert "require_upstream_origin" in update
    assert "status --porcelain" in update
    assert "${INSTALL_DIR} is not a Git checkout" in update


def test_update_stops_both_persistent_compose_services():
    declared = re.search(
        r"readonly CONTROL_PLANE_SERVICES=\((.*?)\)", _script(), re.DOTALL
    )
    assert declared is not None, "install.sh no longer declares CONTROL_PLANE_SERVICES"
    assert declared.group(1).split() == ["blitzecdn-api", "blitzecdn-worker"]


# --- update: sandboxed behaviour ---------------------------------------------
#
# Everything up to the point of no return runs for real here. Past it the
# command rebuilds the virtualenv, which downloads uv and resolves a lockfile —
# too heavy for a unit test and covered instead by the container lifecycle.
# So each test below drives the run into a documented refusal and asserts the
# host was left alone.


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

    Driven into the checkout failure so the run stops before the rebuild, which
    would download uv and resolve a lockfile.
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
    assert "the containers are stopped" in result.stderr, (
        "past the point of no return, the refusal must say how to recover"
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


def test_update_backs_up_and_stops_services_in_that_order(tmp_path: Path):
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
    recorded = log.read_text(encoding="utf-8").splitlines()
    assert recorded[0] == "backup"
    assert "stop blitzecdn-api blitzecdn-worker" in recorded
    assert "checkout" not in recorded


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
    assert "backup" not in log.read_text(encoding="utf-8").splitlines()


def test_the_default_install_ends_by_installing_the_wrapper():
    """Otherwise the CLI is only reachable from inside the checkout."""
    install = _section("install")
    assert "handoff_user_wrapper" in install


def test_every_command_uses_the_shared_option_parser():
    """One parser, and one table that routes every command through it.

    Asserted against the sourced script rather than its text: the command table
    is the declaration now, so the property is that it has a row per command,
    not that the file contains five dispatch lines.
    """
    assert _script().count("while [[ $# -gt 0 ]]") == 1
    for command in ("install", "standalone", "uninstall", "fresh", "update"):
        assert _query(f"command_field {command} 2") != "", (
            f"{command} has no row in COMMAND_TABLE"
        )


def test_both_capability_renderings_come_from_one_list():
    """The uv extras and the Ansible list must name the same capabilities.

    They are consumed by different things — one builds the virtualenv, the
    other tells the role which capability configuration to write — and a
    controller given configuration for a capability it does not have refuses to
    start. Each was expanded by hand before, so drift was a live possibility.
    """
    for override in ("", "backup cache"):
        prelude = f'export BLITZECDN_CAPABILITIES="{override}"\n' if override else ""
        extras = _query(f"{prelude}capability_extras").split()
        rendered = _query(f"{prelude}capability_json")

        # "--extra name --extra name ..." -> the names alone.
        assert extras[::2] == ["--extra"] * (len(extras) // 2)
        from_extras = extras[1::2]
        from_json = [name.strip('"') for name in rendered.split(",")]

        assert from_extras == from_json
        if override:
            assert from_extras == override.split()


def test_the_installer_never_demands_a_value_no_capability_will_read():
    """`--email` is required, and it is blitzecdn-certificates' configuration.

    The address is written into the managed blitzecdn.toml only under
    `certificates`, and the role asserts one is present only under
    `certificates`. With that capability out of the default list the installer
    still refused to run without `--email`, validated the address, passed it to
    Ansible — and the template dropped it, leaving an operator who supplied an
    ACME account on a controller that could not issue a certificate for any
    site it served. Requiring a value and installing nothing that reads it is
    the contradiction; this is the direction that bites, because the installer
    is where the operator is told the value matters.
    """
    assert 'die 2 "error: --email is required"' in _script()
    assert "certificates" in _query("capabilities").split()


def test_the_control_plane_image_carries_the_capabilities_it_is_installed_with():
    """The image's extras and install.sh's list are one decision, spelled twice.

    They are read by different things — the extras build the virtualenv the
    control plane actually runs in, the list decides which capability
    configuration the managed blitzecdn.toml carries — and each direction of
    drift fails silently in its own way. Configuration for a capability the
    image lacks is a control plane that refuses to start at all; a capability
    in the image the list omits is one nothing ever configures, which is how
    the image came to install certbot for a distribution it did not have.

    Read from the instruction rather than the file: a comment naming a
    capability is not an `--extra` for it.
    """
    dockerfile = (
        REPO_ROOT / "src/blitzecdn/docker/control-plane/Dockerfile"
    ).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )
    sync = re.search(r"RUN uv sync .*?(?=\n[A-Z])", code, re.DOTALL)
    assert sync, "the image no longer builds its virtualenv with `uv sync`"
    extras = re.findall(r"--extra\s+(\S+)", sync.group())

    assert sorted(extras) == sorted(_query("capabilities").split())


def test_the_development_recipe_installs_what_a_server_actually_gets():
    """`just install-prod` says it installs what a server gets; it must.

    It is the only local configuration that stands in for a real controller, so
    a capability missing from it is one nothing is ever tested against — the
    recipe would quietly narrow what "production" means for everyone reading
    its output.
    """
    justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    recipe = re.search(r"\ninstall-prod:\n(.*?)(?=\n\S)", justfile, re.DOTALL)
    assert recipe, "install-prod is no longer a recipe"

    extras = re.findall(r"--extra\s+(\S+)", recipe.group(1))
    assert sorted(extras) == sorted(_query("capabilities").split())


def test_the_control_plane_role_defaults_to_the_list_the_installer_passes():
    """The role's fallback, for anyone converging it without install.sh.

    install.sh always passes the list, so this default is only reached by a
    playbook that does not — and a default that had drifted would hand such a
    run a blitzecdn.toml describing a controller it is not.
    """
    defaults = yaml.safe_load(
        (CORE_ANSIBLE / "roles/blitzecdn_controlplane/defaults/main.yml").read_text(
            encoding="utf-8"
        )
    )
    spec = yaml.safe_load(
        (
            CORE_ANSIBLE / "roles/blitzecdn_controlplane/meta/argument_specs.yml"
        ).read_text(encoding="utf-8")
    )
    declared = spec["argument_specs"]["main"]["options"]
    expected = sorted(_query("capabilities").split())

    assert sorted(defaults["blitzecdn_controlplane_capabilities"]) == expected
    assert (
        sorted(declared["blitzecdn_controlplane_capabilities"]["default"]) == expected
    )


def test_the_command_table_and_the_help_agree_on_every_option():
    """The table is what accepts an option; the help is what advertises it.

    They were separate lists and could disagree — an option the parser took but
    no help mentioned, or the reverse. Neither is reachable now without this
    failing.
    """
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
    # The copy deletes itself on the way out. It registers with the shared
    # cleanup stack rather than setting its own EXIT trap, which would replace
    # the handler and strand everything else the run had asked to clean up.
    assert 'cleanup_paths+=("$0")' in helper
    code = [line for line in helper.splitlines() if not line.lstrip().startswith("#")]
    assert not any(line.lstrip().startswith("trap ") for line in code), (
        "a second EXIT trap here replaces the shared cleanup handler"
    )
