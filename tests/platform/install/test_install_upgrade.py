"""`upgrade`: crossing a major line, which `update` refuses to do."""

from __future__ import annotations

from pathlib import Path

from support.install import (
    run_sandboxed,
    script_function,
    update_sandbox,
)

# --- upgrade: crossing a major line, which `update` refuses to do ----------


def test_upgrade_targets_the_next_major_line_and_only_the_next(tmp_path: Path):
    """A v3 host goes to the newest v4, never straight to v5.

    Each major announces its deprecations in the line before it, so upgrades go
    one major at a time. The stub offers a v5 line that must be ignored.
    """
    script, stubs, _ = update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = run_sandboxed(
        script,
        "upgrade",
        "--yes",
        env_extra={
            "UPDATE_PROJECT_VERSION": "3.0.0",
            "UPDATE_GIT_EXACT_TAG": "v3.0.0",
            "UPDATE_GIT_TAGS": "v5.0.0\nv4.1.0\nv4.0.0\nv3.0.0\n",
            "UPDATE_ORDER_LOG": str(log),
            # Stopped at the checkout, as the update suite does: everything
            # past it rebuilds a real project this sandbox does not have.
            "UPDATE_GIT_CHECKOUT_FAILS": "1",
        },
        bin_dir=stubs,
    )

    assert "Checking out v4.1.0" in result.stdout
    assert "v5" not in result.stdout


def test_upgrade_refuses_a_host_that_has_not_finished_its_own_line(tmp_path: Path):
    """The deprecation window is the releases you would be skipping."""
    script, stubs, _ = update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = run_sandboxed(
        script,
        "upgrade",
        "--yes",
        env_extra={
            "UPDATE_PROJECT_VERSION": "3.0.0",
            "UPDATE_GIT_EXACT_TAG": "v3.0.0",
            "UPDATE_GIT_TAGS": "v4.0.0\nv3.2.0\nv3.0.0\n",
            "UPDATE_ORDER_LOG": str(log),
        },
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "v3.2.0 is the newest v3.x release" in result.stderr
    assert "update" in result.stderr, "and say which command gets there"
    assert not log.exists(), "nothing may be stopped or checked out"


def test_upgrade_refuses_when_there_is_no_next_line(tmp_path: Path):
    """The newest major there is, said as that rather than as a failure."""
    script, stubs, _ = update_sandbox(tmp_path)

    result = run_sandboxed(
        script,
        "upgrade",
        "--yes",
        env_extra={
            "UPDATE_PROJECT_VERSION": "3.0.0",
            "UPDATE_GIT_EXACT_TAG": "v3.0.0",
            "UPDATE_GIT_TAGS": "v3.0.0\n",
        },
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "no v4.x release tag" in result.stderr
    assert "nothing was changed" in result.stderr


def test_upgrade_backs_up_everything_not_just_the_database(tmp_path: Path):
    """`update` protects what a migration can damage; a major moves more."""
    script, stubs, _ = update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    run_sandboxed(
        script,
        "upgrade",
        "--yes",
        env_extra={
            "UPDATE_PROJECT_VERSION": "3.0.0",
            "UPDATE_GIT_EXACT_TAG": "v3.0.0",
            "UPDATE_GIT_TAGS": "v4.0.0\nv3.0.0\n",
            "UPDATE_ORDER_LOG": str(log),
            "UPDATE_BACKUP_ARGS_LOG": str(tmp_path / "backup-args.log"),
            "UPDATE_GIT_CHECKOUT_FAILS": "1",
        },
        bin_dir=stubs,
    )

    assert log.read_text(encoding="utf-8").splitlines() == ["backup"], (
        "a backup, nothing stopped, and the checkout driven into failure"
    )

    asked = (tmp_path / "backup-args.log").read_text(encoding="utf-8")
    assert asked.strip() == "backup create", (
        "a major upgrade backs up everything; --only database is `update`'s bargain"
    )


def test_upgrade_does_not_ask_the_commit_graph_whether_it_may_cross(tmp_path: Path):
    """A major line branches and then moves on its own.

    Ancestry is right within one line, but across two it would refuse once the old
    line took a fix; the guarantee here is version arithmetic.
    """
    script, stubs, _ = update_sandbox(tmp_path)

    result = run_sandboxed(
        script,
        "upgrade",
        "--yes",
        env_extra={
            "UPDATE_PROJECT_VERSION": "3.0.0",
            "UPDATE_GIT_EXACT_TAG": "v3.0.0",
            "UPDATE_GIT_TAGS": "v4.0.0\nv3.0.0\n",
            "UPDATE_GIT_ANCESTOR_STATUS": "1",
            "UPDATE_GIT_CHECKOUT_FAILS": "1",
        },
        bin_dir=stubs,
    )

    assert "Checking out v4.0.0" in result.stdout, (
        "a non-ancestor checkout must still reach the checkout; `update` "
        "refuses one here and an upgrade may not"
    )
    assert "not an ancestor" not in result.stderr


def test_upgrade_confirmation_wants_the_version_typed(tmp_path: Path):
    """Not y/N: the operator should have read where they are going."""
    body = script_function("confirm_release_move")

    assert "Type ${target} to continue" in body
    assert '[[ ${answer} == "${target}" ]]' in body


def test_upgrade_names_what_a_major_may_have_taken_away(tmp_path: Path):
    """The prompt lists the surfaces, because they are what an operator owns."""
    body = script_function("confirm_release_move")

    unwrapped = " ".join(body.split())
    for surface in ("Ansible variables", "CLI commands", "API routes"):
        assert surface in unwrapped, surface
