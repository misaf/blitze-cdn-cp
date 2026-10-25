"""`install_release`: which release `update` and `upgrade` move a host to.

Driven through a fake Git that answers from a table, so each rule is asserted
as an answer rather than as a string in a script. The sandboxed suites in
`test_install_update.py` and `test_install_upgrade.py` run the same module
behind the real installer.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Mapping

import pytest
from support.paths import SOURCE

from blitzecdn.host.install_release import (
    NOTHING_TO_DO,
    Plan,
    RefusedError,
    installed_release,
    main,
    newest_release,
    plan_update,
    plan_upgrade,
    project_version,
)

REPOSITORY = "/opt/blitzecdn"


def fake_git(
    *,
    version: str | None = "3.0.0",
    tags: str = "v3.1.0\nv3.0.0",
    exact: str | None = "v3.0.0",
    nearest: str | None = "v3.0.0",
    ancestor: bool = True,
    commits: str | None = "12",
    drift: str = "4",
    sha: str = "abc1234",
):
    """A Git that answers the questions the selector asks, and nothing else."""

    def git(*arguments: str) -> str | None:
        match arguments:
            case ("show", "HEAD:pyproject.toml"):
                return None if version is None else f'name = "x"\nversion = "{version}"'
            case ("tag", "--list", pattern, "--sort=-v:refname"):
                prefix = pattern.removesuffix("*")
                return "\n".join(t for t in tags.splitlines() if t.startswith(prefix))
            case ("describe", "--tags", "--exact-match", "HEAD"):
                return exact
            case ("describe", "--tags", "--abbrev=0", "HEAD"):
                return nearest
            case ("merge-base", "--is-ancestor", "HEAD", _):
                return "" if ancestor else None
            case ("rev-list", "--count", range_) if range_.startswith("HEAD.."):
                return commits
            case ("rev-list", "--count", _):
                return drift
            case ("rev-parse", "--short", "HEAD"):
                return sha
        raise AssertionError(f"unexpected git {arguments}")

    return git


def test_the_version_is_read_from_pyproject_at_head():
    assert project_version(fake_git(version="3.4.5")) == "3.4.5"
    assert project_version(fake_git(version=None)) is None


def test_only_complete_release_tags_in_the_line_are_installable():
    """A pre-release or a branch-shaped tag is never what a server runs."""
    git = fake_git(tags="v3.2.0-rc1\nv3.x\nv3.1.0\nv3.0.0\nv4.0.0")
    assert newest_release(git, 3) == "v3.1.0"
    assert newest_release(git, 5) is None


@pytest.mark.parametrize(
    ("answers", "described"),
    [
        ({"exact": "v3.0.0"}, "v3.0.0"),
        ({"exact": None, "nearest": "v3.0.0", "drift": "4"}, "v3.0.0 (+4 commits)"),
        ({"exact": None, "nearest": None, "sha": "abc1234"}, "v3.0.0 (abc1234)"),
    ],
)
def test_a_host_names_the_release_it_is_on_and_never_pretends(
    answers: Mapping[str, str | None], described: str
):
    assert installed_release(fake_git(**answers)) == described  # type: ignore[arg-type]


def test_an_update_moves_to_the_newest_tag_in_its_own_line():
    plan = plan_update(fake_git(tags="v4.0.0\nv3.1.0\nv3.0.0"), REPOSITORY)
    assert plan == Plan(current="v3.0.0", target="v3.1.0", commits="12")


def test_an_update_on_the_newest_release_has_nothing_to_do():
    plan = plan_update(fake_git(exact="v3.1.0"), REPOSITORY)
    assert plan.current == plan.target == "v3.1.0"


@pytest.mark.parametrize(
    ("answers", "says"),
    [
        ({"version": None}, "could not read the project version"),
        ({"tags": "v4.0.0"}, "no v3.x release tag"),
        ({"ancestor": False}, "is not an ancestor of v3.1.0"),
        ({"commits": None}, "could not count the commits"),
    ],
)
def test_an_update_refuses_before_anything_changes(
    answers: Mapping[str, object], says: str
):
    with pytest.raises(RefusedError) as refused:
        plan_update(fake_git(**answers), REPOSITORY)  # type: ignore[arg-type]
    assert says in refused.value.args[0]
    assert "nothing was changed" in refused.value.args[0]


def test_an_upgrade_crosses_one_line_and_only_one():
    git = fake_git(tags="v5.0.0\nv4.1.0\nv4.0.0\nv3.0.0", exact="v3.0.0")
    assert plan_upgrade(git).target == "v4.1.0"


def test_an_upgrade_does_not_ask_the_commit_graph():
    git = fake_git(tags="v4.0.0\nv3.0.0", ancestor=False)
    assert plan_upgrade(git).target == "v4.0.0"


def test_an_upgrade_starts_from_the_newest_release_of_its_line():
    git = fake_git(tags="v4.0.0\nv3.2.0\nv3.0.0", exact="v3.0.0")
    with pytest.raises(RefusedError) as refused:
        plan_upgrade(git)
    assert "v3.2.0 is the newest v3.x release" in refused.value.args[0]
    assert "./install.sh update" in " ".join(refused.value.args)


def test_an_upgrade_from_the_newest_line_says_so():
    with pytest.raises(RefusedError) as refused:
        plan_upgrade(fake_git(tags="v3.0.0"))
    assert "no v4.x release tag" in refused.value.args[0]
    assert "newest major line" in refused.value.args[1]


def test_the_plan_reaches_the_script_as_key_value_lines(monkeypatch, capsys):
    monkeypatch.setattr(
        sys.modules["blitzecdn.host.install_release"], "git_in", lambda _: fake_git()
    )
    assert main(["update", "--repository", REPOSITORY]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "current=v3.0.0",
        "target=v3.1.0",
        "commits=12",
    ]

    monkeypatch.setattr(
        sys.modules["blitzecdn.host.install_release"],
        "git_in",
        lambda _: fake_git(exact="v3.1.0"),
    )
    assert main(["update", "--repository", REPOSITORY]) == NOTHING_TO_DO
    assert "Already on v3.1.0; nothing to update." in capsys.readouterr().out


def test_the_selector_runs_on_a_bare_interpreter():
    """The host's python3 runs it before the release it belongs to is installed."""
    tree = ast.parse((SOURCE / "host/install_release.py").read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}
