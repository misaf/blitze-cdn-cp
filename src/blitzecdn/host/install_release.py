"""Which release `install.sh update` and `upgrade` move a host to.

Run by the host's own ``python3 -I`` from the checkout being moved, before
anything is checked out or stopped, so it imports nothing but the standard
library: on an update the virtualenv belongs to the release being left, and
the application image is not on the host's interpreter at all.
``install_handoff`` is its counterpart on the other side of the move.

Every answer comes from Git. A host follows its own release line, the target is
the newest complete ``vMAJOR.MINOR.PATCH`` tag in it, and a pre-release or a
branch-shaped tag is never installable. ``update`` stays inside the line and
only moves forward; ``upgrade`` crosses to the next line and only from the
newest release of the current one, because a major's deprecations are
announced in the line being left.

The plan is printed as ``key=value`` lines for the script to read. Exit status
1 is a refusal and :data:`NOTHING_TO_DO` an update that is already done; either
way the message has been printed and nothing on the host was changed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: The exit status for an update with nothing to do.
NOTHING_TO_DO = 3

_VERSION = re.compile(r'^version\s*=\s*"(\d+\.\d+\.\d+)"')
_RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")

#: Runs one Git command against the checkout; ``None`` when it failed.
Git = Callable[..., "str | None"]


class RefusedError(Exception):
    """The move cannot happen. Each argument is one line for the operator."""


@dataclass(frozen=True, slots=True)
class Plan:
    current: str
    target: str
    commits: str


def git_in(repository: str) -> Git:
    """Git against the installation checkout.

    ``safe.directory`` because the working tree belongs to the installation's
    account while the installer runs as root.
    """

    def run(*arguments: str) -> str | None:
        result = subprocess.run(  # noqa: S603 - fixed executable, argument list
            ["git", "-c", f"safe.directory={repository}", "-C", repository, *arguments],  # noqa: S607 - git from PATH, as the installer runs it
            check=False,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    return run


def project_version(git: Git) -> str | None:
    """The release version recorded in ``pyproject.toml`` at HEAD.

    A tag name and this version are one statement about which release this is:
    CI refuses to build a tag whose name is not ``v`` plus this field.
    """
    for line in (git("show", "HEAD:pyproject.toml") or "").splitlines():
        if match := _VERSION.match(line):
            return match.group(1)
    return None


def newest_release(git: Git, major: int) -> str | None:
    """The newest complete release tag in one major line.

    The glob is anchored to the major, so no answer can come from another line
    whatever the origin carries.
    """
    listed = git("tag", "--list", f"v{major}.*", "--sort=-v:refname") or ""
    return next((tag for tag in listed.splitlines() if _RELEASE_TAG.match(tag)), None)


def installed_release(git: Git) -> str:
    """The release this host is on, as an operator would name it.

    A tagged checkout is the bare tag. A checkout past a tag says how far past,
    and one with no tag behind it gives the recorded version and the commit, so
    a host that is not on a release never claims to be.
    """
    if tag := git("describe", "--tags", "--exact-match", "HEAD"):
        return tag
    if base := git("describe", "--tags", "--abbrev=0", "HEAD"):
        drift = git("rev-list", "--count", f"{base}..HEAD") or "?"
        return f"{base} (+{drift} commits)"
    version = project_version(git) or "unknown"
    return f"v{version} ({git('rev-parse', '--short', 'HEAD') or 'unknown'})"


def _major(git: Git) -> int:
    version = project_version(git)
    if version is None:
        raise RefusedError(
            "error: could not read the project version from pyproject.toml at "
            "HEAD; nothing was changed"
        )
    return int(version.split(".", 1)[0])


def plan_update(git: Git, repository: str) -> Plan:
    """The newest release in this host's own line.

    A plan whose target is where the host already is means there is nothing to
    do; the commit count is then ``0``.
    """
    major = _major(git)
    target = newest_release(git, major)
    if target is None:
        raise RefusedError(
            f"error: no v{major}.x release tag exists in the fetched repository; "
            "nothing was changed",
            "Only tagged releases are installable. "
            f"Check 'git -C {repository} tag --list'.",
        )
    current = installed_release(git)
    # Only an exact tag can equal the target: the other two descriptions carry
    # a commit count or a SHA.
    if current == target:
        return Plan(current=current, target=target, commits="0")
    # Forward only. A checkout that is not an ancestor of the release has
    # commits origin does not, or is already past it, and moving it would
    # discard them.
    if git("merge-base", "--is-ancestor", "HEAD", f"{target}^{{commit}}") is None:
        raise RefusedError(
            f"error: {repository} is not an ancestor of {target}; nothing was changed",
            "The checkout has commits origin does not, or is on a newer release.",
            "Use --fresh to rebuild from origin.",
        )
    commits = git("rev-list", "--count", f"HEAD..{target}")
    if commits is None:
        raise RefusedError(
            f"error: could not count the commits up to {target}; nothing was changed"
        )
    return Plan(current=current, target=target, commits=commits)


def plan_upgrade(git: Git) -> Plan:
    """The newest release of the next major line, and only the next.

    Asking only for ``major + 1`` is what makes one line at a time structural:
    nothing here ever looks at the line after. There is no ancestry check, as
    a major line branches from the one before it and then moves on its own.
    """
    major = _major(git)
    target = newest_release(git, major + 1)
    if target is None:
        raise RefusedError(
            f"error: no v{major + 1}.x release tag exists in the fetched "
            "repository; nothing was changed",
            "This host is on the newest major line there is.",
        )
    current = installed_release(git)
    newest_in_line = newest_release(git, major)
    if newest_in_line is not None and current != newest_in_line:
        raise RefusedError(
            f"error: this host is on {current}, and {newest_in_line} is the "
            f"newest v{major}.x release",
            "Run './install.sh update' first: a major's deprecations are announced in",
            "the line you are leaving, and crossing early skips them.",
        )
    commits = git("rev-list", "--count", f"HEAD..{target}") or "unknown"
    return Plan(current=current, target=target, commits=commits)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install-release")
    parser.add_argument("command", choices=("update", "upgrade"))
    parser.add_argument("--repository", required=True)
    options = parser.parse_args(arguments)
    git = git_in(options.repository)
    try:
        plan = (
            plan_update(git, options.repository)
            if options.command == "update"
            else plan_upgrade(git)
        )
    except RefusedError as refused:
        print(*refused.args, sep="\n", file=sys.stderr)
        return 1
    if plan.current == plan.target:
        print(f"Already on {plan.target}; nothing to update.")
        return NOTHING_TO_DO
    print(f"current={plan.current}\ntarget={plan.target}\ncommits={plan.commits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
