"""The controller's release probe token: created once, private, and stable."""

from __future__ import annotations

import re
import stat

import pytest

from blitzecdn.core.ansible.probe import release_probe_token


def test_the_token_is_created_once_and_then_reused(tmp_path):
    first = release_probe_token(tmp_path)

    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert release_probe_token(tmp_path) == first


def test_the_token_file_is_readable_by_its_owner_only(tmp_path):
    release_probe_token(tmp_path)

    mode = (tmp_path / "release-probe.token").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_a_damaged_token_file_is_refused_rather_than_replaced(tmp_path):
    """Every edge holds the old value, so silently replacing it hides the cause."""
    (tmp_path / "release-probe.token").write_text("not a token\n")

    with pytest.raises(RuntimeError, match="release probe token"):
        release_probe_token(tmp_path)


def test_a_competing_creator_keeps_the_token_already_published(tmp_path):
    """The loser of a first-use race reads the winner's token, never replaces it."""
    from blitzecdn.core.ansible import probe

    winner = "b" * 64
    (tmp_path / "release-probe.token").write_text(winner + "\n")

    assert probe._create(tmp_path / "release-probe.token") == winner
    assert (tmp_path / "release-probe.token").read_text() == winner + "\n"
    # The candidate it wrote first is never left behind.
    assert [child.name for child in tmp_path.iterdir()] == ["release-probe.token"]


def test_the_edge_play_installs_the_token_even_for_a_sealed_release():
    """Redeploying an unchanged release must repair an edge with an old token.

    The nginx role is skipped once a release is sealed, so the token is
    reconciled by its own import, gated only on rollback and commit.
    """
    from pathlib import Path

    import yaml

    import blitzecdn

    ansible = Path(blitzecdn.__file__).parent / "assets/ansible"
    (play,) = yaml.safe_load((ansible / "playbooks/edge.yml").read_text())
    (task,) = [
        task
        for task in play["pre_tasks"]
        if task.get("ansible.builtin.import_role", {}).get("tasks_from")
        == "release-probe.yml"
    ]
    assert "seal" not in str(task.get("when", ""))
    assert "always" in task["tags"]
    main = (ansible / "roles/blitzecdn_nginx/tasks/main.yml").read_text()
    assert "release_probe" not in main
