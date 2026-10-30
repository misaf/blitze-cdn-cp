"""Which release `install.sh update` moves a host to.

Run by `install.sh` in the installed control-plane image, so a host needs no
copy of it. The image tag is the release: the registry publishes bare
``MAJOR.MINOR.PATCH`` tags beside commit tags, and only complete versions take
part. ``update`` moves forward to the newest release of the installed line;
``update --major`` crosses to the next line, only from the newest release of
the current one. A target the operator names is checked for direction instead.
Prints the target version; exit 1 refuses with one message on stderr.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Sequence
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REGISTRY = "ghcr.io"
REPOSITORY = "misaf/blitzecdn-control-plane"

_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

Version = tuple[int, int, int]


class ReleaseSelectionError(ValueError):
    """A safe forward release move could not be selected."""


def parse_version(tag: str) -> Version | None:
    """A complete release tag, or None for prereleases and commit tags."""
    match = _VERSION.fullmatch(tag)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def _installed(current: str) -> Version:
    installed = parse_version(current)
    if installed is None:
        raise ReleaseSelectionError(
            f"the installed image is not a release: its tag is {current!r}"
        )
    return installed


def _format(version: Version) -> str:
    return ".".join(map(str, version))


def select_release(current: str, tags: Iterable[str], *, major: bool = False) -> str:
    """The newest release in the installed line, or in the next line.

    A major move requires the newest release of the installed line first, so
    its deprecation window is not skipped. Neither ever moves backwards.
    """
    installed = _installed(current)
    versions = {version for tag in tags if (version := parse_version(tag)) is not None}
    same_line = [version for version in versions if version[0] == installed[0]]
    if not same_line:
        raise ReleaseSelectionError(f"no {installed[0]}.x release is published")
    newest = max(same_line)
    if newest < installed:
        raise ReleaseSelectionError(
            f"the newest published {installed[0]}.x release is {_format(newest)}, "
            f"older than the installed {current}"
        )
    if not major:
        return _format(newest)
    if installed != newest:
        raise ReleaseSelectionError(
            f"this host is on {current}, and {_format(newest)} is the newest "
            f"{installed[0]}.x release; run 'update' first: a major's deprecations "
            "are announced in the line you are leaving"
        )
    next_line = [version for version in versions if version[0] == installed[0] + 1]
    if not next_line:
        raise ReleaseSelectionError(
            f"no {installed[0] + 1}.x release is published; this host is on the "
            "newest major line there is"
        )
    return _format(max(next_line))


def check_target(current: str, target: str, *, major: bool = False) -> str:
    """A named target, refused unless it moves the way the command does."""
    installed = _installed(current)
    wanted = parse_version(target)
    if wanted is None:
        raise ReleaseSelectionError(f"{target!r} is not a release version")
    if major:
        if wanted[0] != installed[0] + 1:
            raise ReleaseSelectionError(
                f"{target} is not in the next major line, {installed[0] + 1}.x"
            )
    elif wanted[0] != installed[0]:
        raise ReleaseSelectionError(
            f"{target} is not in the installed {installed[0]}.x line; "
            "crossing a major line is 'update --major'"
        )
    elif wanted < installed:
        raise ReleaseSelectionError(
            f"{target} is older than the installed {current}; "
            "an update never moves backwards"
        )
    return target


class _NoRedirect(HTTPRedirectHandler):
    """A redirect would carry the bearer token to wherever it points."""

    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def registry_tags() -> list[str]:
    """Every tag of the public control-plane image, read anonymously.

    Pagination is followed only within the same registry endpoint, so a
    response cannot send the token to another host.
    """
    endpoint = f"https://{REGISTRY}/v2/{REPOSITORY}/tags/list"
    token_url = f"https://{REGISTRY}/token?" + urlencode(
        {"scope": f"repository:{REPOSITORY}:pull", "service": REGISTRY}
    )
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(token_url, timeout=15) as response:
            token = json.load(response)["token"]
        if not isinstance(token, str) or not token:
            raise ValueError("invalid registry token")
        tags: list[str] = []
        url = endpoint + "?n=100"
        visited: set[str] = set()
        while url:
            if url in visited:
                raise ValueError("registry pagination loop")
            visited.add(url)
            request = Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310
            with opener.open(request, timeout=15) as response:
                page_tags = json.load(response).get("tags") or []
                if not isinstance(page_tags, list) or not all(
                    isinstance(tag, str) for tag in page_tags
                ):
                    raise ValueError("invalid registry tag list")
                tags.extend(page_tags)
                link = response.headers.get("Link", "")
            if not link:
                break
            match = re.fullmatch(r'\s*<([^>]+)>\s*;\s*rel="?next"?\s*', link)
            if not match:
                raise ValueError("invalid registry pagination link")
            url = urljoin(endpoint, match.group(1))
            parsed, expected = urlsplit(url), urlsplit(endpoint)
            if (
                (parsed.scheme, parsed.netloc, parsed.path)
                != (expected.scheme, expected.netloc, expected.path)
                or parsed.fragment
                or set(parse_qs(parsed.query)) - {"n", "last"}
            ):
                raise ValueError("unsafe registry pagination link")
        return tags
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ReleaseSelectionError(
            f"could not list the releases on {REGISTRY}: {error}"
        ) from error


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="image-releases")
    parser.add_argument("current", help="the installed image's release version")
    parser.add_argument("--major", action="store_true")
    parser.add_argument("--target", help="a release the operator named")
    options = parser.parse_args(arguments)
    try:
        if options.target is not None:
            target = check_target(options.current, options.target, major=options.major)
        else:
            target = select_release(
                options.current, registry_tags(), major=options.major
            )
    except ReleaseSelectionError as refused:
        print(f"error: {refused}; nothing was changed", file=sys.stderr)
        return 1
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
