"""`image_releases`: which release `update` and `update --major` move a host to.

Run by the installer in the installed control-plane image before anything on
the host changes, so every refusal here costs no downtime.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from support.install import host_helper

from blitzecdn.host import image_releases
from blitzecdn.host.image_releases import (
    ReleaseSelectionError,
    check_target,
    main,
    parse_version,
    registry_tags,
    select_release,
)

# --- choosing from the published tags ---------------------------------------


def test_only_complete_versions_are_releases():
    """Commit tags and prereleases are published beside them and never chosen."""
    assert parse_version("6.10.0") == (6, 10, 0)
    for tag in ("6.99.0-rc1", "latest", "deadbeef", "v6.1.0", "06.1.0", "6.1"):
        assert parse_version(tag) is None, tag


def test_update_selects_the_newest_release_in_the_installed_line():
    """Numerically: 6.10.0 is newer than 6.9.0. Never the next major."""
    tags = ["6.9.0", "6.10.0", "7.0.0", "6.99.0-rc1", "latest", "deadbeef"]
    assert select_release("6.9.0", tags) == "6.10.0"


def test_update_on_the_newest_release_selects_it():
    """The installer reads an unchanged version as nothing to do."""
    assert select_release("6.2.0", ["6.1.0", "6.2.0"]) == "6.2.0"


def test_a_major_update_moves_one_line_only_from_the_newest_of_its_own():
    tags = ["6.1.0", "6.2.0", "7.0.0", "7.3.0", "8.0.0"]
    with pytest.raises(ReleaseSelectionError, match=r"6\.2\.0 is the newest 6\.x"):
        select_release("6.1.0", tags, major=True)
    assert select_release("6.2.0", tags, major=True) == "7.3.0"


def test_a_major_update_says_when_there_is_no_next_line():
    with pytest.raises(ReleaseSelectionError, match=r"no 7\.x release is published"):
        select_release("6.0.0", ["6.0.0", "8.0.0"], major=True)


@pytest.mark.parametrize(
    ("current", "tags", "message"),
    [
        ("local", ["6.0.0"], "is not a release"),
        ("6.2.0", ["6.1.0"], "older than the installed 6.2.0"),
        ("6.0.0", ["7.0.0"], "no 6.x release is published"),
    ],
)
def test_selection_refuses_what_it_cannot_move_forward_from(current, tags, message):
    with pytest.raises(ReleaseSelectionError, match=message):
        select_release(current, tags)


# --- a target the operator names ---------------------------------------------


def test_a_named_target_in_the_installed_line_is_accepted():
    assert check_target("6.1.0", "6.3.0") == "6.3.0"
    assert check_target("6.1.0", "6.1.0") == "6.1.0"


def test_a_named_target_in_the_next_line_needs_major():
    with pytest.raises(ReleaseSelectionError, match="update --major"):
        check_target("6.1.0", "7.0.0")
    assert check_target("6.1.0", "7.0.0", major=True) == "7.0.0"


@pytest.mark.parametrize(
    ("target", "major", "message"),
    [
        ("6.0.9", False, "never moves backwards"),
        ("5.9.0", False, "not in the installed 6.x line"),
        ("8.0.0", True, "not in the next major line, 7.x"),
        ("6.2.0", True, "not in the next major line, 7.x"),
        ("latest", False, "not a release version"),
    ],
)
def test_a_named_target_is_refused_unless_it_moves_the_way_asked(
    target: str, major: bool, message: str
):
    with pytest.raises(ReleaseSelectionError, match=message):
        check_target("6.1.0", target, major=major)


def test_a_named_target_does_not_ask_the_registry(monkeypatch, capsys):
    """A mirror's host may not reach ghcr.io at all."""

    def unreachable() -> list[str]:
        raise AssertionError("the registry was asked")

    monkeypatch.setattr(image_releases, "registry_tags", unreachable)
    assert main(["6.1.0", "--target", "6.2.0"]) == 0
    assert capsys.readouterr().out == "6.2.0\n"


def test_a_refusal_is_one_line_saying_nothing_changed(monkeypatch, capsys):
    monkeypatch.setattr(image_releases, "registry_tags", lambda: ["6.1.0", "7.0.0"])
    assert main(["6.0.0", "--major"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error: this host is on 6.0.0, and 6.1.0 is the newest 6.x release; run "
        "'update' first: a major's deprecations are announced in the line you are "
        "leaving; nothing was changed\n"
    )


def test_the_helper_runs_on_a_bare_interpreter():
    """The installer runs it as `python -I`: standard library only."""
    result = host_helper("image_releases", "6.1.0", "--target", "6.0.0")
    assert result.returncode == 1
    assert "never moves backwards" in result.stderr


# --- reading the registry -----------------------------------------------------


class _Response(io.BytesIO):
    def __init__(self, body: Any, link: str = "") -> None:
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers = {"Link": link} if link else {}


class _Opener:
    def __init__(self, *pages: _Response) -> None:
        self.pages = [_Response({"token": "secret"}), *pages]
        self.calls: list[Any] = []

    def open(self, request: Any, timeout: float) -> _Response:
        self.calls.append(request)
        return self.pages[len(self.calls) - 1]


def test_registry_paginates_without_sending_the_token_elsewhere(monkeypatch):
    opener = _Opener(
        _Response(
            {"tags": ["6.0.0"]},
            "</v2/misaf/blitzecdn-control-plane/tags/list?n=100&last=6.0.0>;"
            ' rel="next"',
        ),
        _Response({"tags": ["6.1.0"]}),
    )
    monkeypatch.setattr(image_releases, "build_opener", lambda *_: opener)

    assert registry_tags() == ["6.0.0", "6.1.0"]
    assert opener.calls[1].get_header("Authorization") == "Bearer secret"
    assert opener.calls[2].full_url.startswith("https://ghcr.io/v2/")


def test_registry_refuses_pagination_to_another_host(monkeypatch):
    opener = _Opener(_Response({"tags": []}, '<https://evil.example/tags>; rel="next"'))
    monkeypatch.setattr(image_releases, "build_opener", lambda *_: opener)

    with pytest.raises(ReleaseSelectionError, match="unsafe registry pagination"):
        registry_tags()
    assert len(opener.calls) == 2


def test_an_unreachable_registry_is_a_refusal(monkeypatch):
    class Offline:
        def open(self, request: Any, timeout: float) -> _Response:
            raise OSError("network is unreachable")

    monkeypatch.setattr(image_releases, "build_opener", lambda *_: Offline())

    with pytest.raises(ReleaseSelectionError, match="could not list the releases"):
        registry_tags()
