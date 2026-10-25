"""Which release `install.sh update`, `upgrade` and `--fresh` move a host to.

Run by the host's ``python3 -I`` from the checkout being left, so standard
library only. A host follows its release line: ``update`` moves forward to the
newest complete ``vMAJOR.MINOR.PATCH`` tag in it; ``upgrade`` crosses to the
next line, only from the newest release of the current one. Both first check
the checkout (upstream origin, no local modifications) and fetch. ``source``
names what ``--fresh`` reinstalls. Prints ``key=value`` lines; exit 1 refuses
and :data:`NOTHING_TO_DO` means current.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: The exit status for an update with nothing to do.
NOTHING_TO_DO = 3

_VERSION = re.compile(r'^version\s*=\s*"(\d+\.\d+\.\d+)"')
_RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")

#: The only origins a host fetches code from and then runs as root.
UPSTREAM_ORIGINS = frozenset(
    {
        "https://github.com/misaf/blitze-cdn-cp",
        "https://github.com/misaf/blitze-cdn-cp.git",
        "git@github.com:misaf/blitze-cdn-cp.git",
    }
)

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
    """Git against the installation checkout, with ``safe.directory`` set.

    Found on ``PATH`` once and run by absolute path; with no git, every answer is
    ``None``, like a failed command.
    """
    executable = shutil.which("git")

    def run(*arguments: str) -> str | None:
        if executable is None:
            return None
        command = [executable, "-c", f"safe.directory={repository}", "-C", repository]
        result = subprocess.run(  # noqa: S603 - fixed executable, argument list
            [*command, *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    return run


def upstream_origin(git: Git, repository: str) -> str:
    """The checkout's origin URL, refused unless it is the upstream repository."""
    url = git("remote", "get-url", "origin")
    if url is None:
        raise RefusedError(f"error: {repository} has no origin remote")
    if url not in UPSTREAM_ORIGINS:
        raise RefusedError(f"error: refusing unexpected origin URL: {url}")
    return url


def prepare(git: Git, repository: str, command: str) -> None:
    """Refuse a checkout this move would damage, then fetch the release tags."""
    upstream_origin(git, repository)
    if git("status", "--porcelain"):
        raise RefusedError(
            f"error: {repository} has local modifications; the {command} would "
            "discard them",
            "Commit or discard them, or use --fresh to rebuild from origin.",
        )
    print("Fetching from origin...", file=sys.stderr)
    if git("fetch", "--tags", "--prune", "origin") is None:
        raise RefusedError("error: could not fetch from origin; nothing was changed")


@dataclass(frozen=True, slots=True)
class Source:
    """What ``--fresh`` clones: a tag or the release branch by name, else a commit."""

    origin: str
    revision: str
    named: bool


def fresh_source(git: Git, repository: str) -> Source:
    """The running checkout's identity, read before the teardown deletes it."""
    origin = upstream_origin(git, repository)
    if tag := git("describe", "--tags", "--exact-match", "HEAD"):
        return Source(origin, tag, named=True)
    # The current major's line, `6.x` for 6.0.0, is the one branch kept attached.
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
    version = project_version(git)
    if branch and version and branch == f"{version.split('.', 1)[0]}.x":
        return Source(origin, branch, named=True)
    commit = git("rev-parse", "HEAD")
    if commit is None:
        raise RefusedError(f"error: could not read HEAD in {repository}")
    return Source(origin, commit, named=False)


def project_version(git: Git) -> str | None:
    """The release version in ``pyproject.toml`` at HEAD; tags must match it."""
    for line in (git("show", "HEAD:pyproject.toml") or "").splitlines():
        if match := _VERSION.match(line):
            return match.group(1)
    return None


def newest_release(git: Git, major: int) -> str | None:
    """The newest complete release tag in one major line, by anchored glob."""
    listed = git("tag", "--list", f"v{major}.*", "--sort=-v:refname") or ""
    return next((tag for tag in listed.splitlines() if _RELEASE_TAG.match(tag)), None)


def installed_release(git: Git) -> str:
    """The release this host is on: the tag, its distance past it, or version+commit."""
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
    """The newest release in this host's own line; ``0`` commits means up to date."""
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
    # Forward only: moving a checkout that is not an ancestor discards commits.
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
    """The newest release of the next major line, and never one further."""
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
    parser.add_argument("command", choices=("update", "upgrade", "source"))
    parser.add_argument("--repository", required=True)
    options = parser.parse_args(arguments)
    git = git_in(options.repository)
    try:
        if options.command == "source":
            source = fresh_source(git, options.repository)
            print(
                f"origin={source.origin}\nrevision={source.revision}\n"
                f"named={int(source.named)}"
            )
            return 0
        prepare(git, options.repository, options.command)
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
