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
