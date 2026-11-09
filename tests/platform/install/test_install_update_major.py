"""`update --major`: crossing a major line, which a plain update refuses to do.

Which release it crosses to is `image_releases`'s, held in
`test_image_releases.py`; these hold what the installer does around it.
"""

from __future__ import annotations

from pathlib import Path

from support.install import (
    FAKE_RELEASE,
    run_sandboxed,
    script_function,
    update_sandbox,
)


def _major(tmp_path: Path, *arguments: str, **env: str):
    script, stubs, _ = update_sandbox(tmp_path)
    log = tmp_path / "order.log"
    selected = tmp_path / "select.log"
    result = run_sandboxed(
        script,
        "update",
        "--major",
        *arguments,
        env_extra={
            "UPDATE_ORDER_LOG": str(log),
            "UPDATE_SELECT_LOG": str(selected),
            "UPDATE_TARGET": "4.1.0",
            **env,
        },
        bin_dir=stubs,
    )
    order = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    selection = selected.read_text(encoding="utf-8") if selected.exists() else ""
    return result, order, selection


def test_a_major_update_asks_the_installed_image_for_the_next_line(tmp_path: Path):
    result, _, selection = _major(tmp_path, "--yes")

    assert selection.split()[-2:] == [FAKE_RELEASE, "--major"]
    assert "to    4.1.0" in result.stderr
    assert "across a major release line" in result.stderr


def test_a_refused_major_update_changes_nothing(tmp_path: Path):
    """The deprecation window is the releases you would be skipping."""
    result, order, _ = _major(
        tmp_path,
        "--yes",
        UPDATE_SELECT_ERROR=(
            "this host is on 3.0.0, and 3.2.0 is the newest 3.x release"
        ),
    )

    assert result.returncode == 1
    assert "3.2.0 is the newest 3.x release" in result.stderr
    assert order == [], "nothing may be backed up, pulled or stopped"


def test_a_major_update_backs_up_everything_not_just_the_database(tmp_path: Path):
    """A plain update protects what a migration can damage; a major moves more."""
    backup_args = tmp_path / "backup-args.log"
    _, order, _ = _major(tmp_path, "--yes", UPDATE_BACKUP_ARGS_LOG=str(backup_args))

    assert order == ["backup", "docker bootstrap"]
    assert backup_args.read_text(encoding="utf-8").strip() == "backup create", (
        "a major update backs up everything; --only database is a plain update's"
    )


def test_a_major_update_wants_the_version_typed(tmp_path: Path):
    """Not y/N: the operator should have read where they are going."""
    script, stubs, _ = update_sandbox(tmp_path)

    refused = run_sandboxed(
        script,
        "update",
        "--major",
        input="y\n",
        env_extra={"UPDATE_TARGET": "4.1.0"},
        bin_dir=stubs,
    )

    assert refused.returncode == 0
    assert "Cancelled" in refused.stdout
    body = script_function("confirm_release_move")
    assert "Type ${target} to continue" in body
    assert '[[ ${answer} == "${target}" ]]' in body


def test_a_major_update_names_what_a_major_may_have_taken_away():
    """The prompt lists the surfaces, because they are what an operator owns."""
    body = script_function("confirm_release_move")

    unwrapped = " ".join(body.split())
    for surface in ("Ansible variables", "CLI commands", "API routes"):
        assert surface in unwrapped, surface
