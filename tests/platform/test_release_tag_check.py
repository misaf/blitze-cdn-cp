"""`scripts/release_tag_check.py` lets only a signed, annotated release tag publish.

Every workflow that publishes from a `v*.*.*` tag runs it first. A lightweight
or unverified tag, a tag naming another version, or a tag whose message is not
`BlitzeCDN vX.Y.Z` is refused before anything reaches the registry.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from types import ModuleType
from typing import Any

from support.paths import REPO_ROOT


def _load() -> ModuleType:
    path = REPO_ROOT / "scripts" / "release_tag_check.py"
    spec = importlib.util.spec_from_file_location("release_tag_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()

COMMIT = "c" * 40
TAG_OBJECT = "t" * 40


def _github(
    *,
    kind: str = "tag",
    points_at: str = COMMIT,
    verified: bool = True,
    reason: str = "valid",
    message: str = "BlitzeCDN v7.1.0\n-----BEGIN PGP SIGNATURE-----\n",
) -> Any:
    responses: dict[str, Mapping[str, Any]] = {
        "git/ref/tags/v7.1.0": {
            "object": {"type": kind, "sha": TAG_OBJECT if kind == "tag" else COMMIT}
        },
        f"git/tags/{TAG_OBJECT}": {
            "object": {"sha": points_at},
            "verification": {"verified": verified, "reason": reason},
            "message": message,
        },
    }
    return responses.__getitem__


def _problems(tag: str = "v7.1.0", version: str = "7.1.0", **github: Any) -> list[str]:
    result: list[str] = check.problems(tag, version, COMMIT, _github(**github))
    return result


def test_a_signed_annotated_tag_with_the_release_message_passes() -> None:
    assert _problems() == []


def test_a_lightweight_tag_is_refused() -> None:
    assert _problems(kind="commit") == [
        "v7.1.0 is a lightweight tag; push an annotated, signed tag "
        "(just release-tag X.Y.Z)"
    ]


def test_a_signature_github_cannot_verify_is_refused() -> None:
    assert _problems(verified=False, reason="unsigned") == [
        "GitHub does not verify the signature on v7.1.0 (unsigned)"
    ]


def test_another_message_is_refused() -> None:
    assert _problems(message="release: 7.1.0\n") == [
        "v7.1.0's message is 'release: 7.1.0', not 'BlitzeCDN v7.1.0'"
    ]
    assert _problems(message="BlitzeCDN 7.1.0\n") == [
        "v7.1.0's message is 'BlitzeCDN 7.1.0', not 'BlitzeCDN v7.1.0'"
    ]


def test_a_tag_for_another_version_or_commit_is_refused() -> None:
    assert _problems(version="7.0.0", message="BlitzeCDN v7.0.0\n") == [
        "release tag 'v7.1.0' must equal v7.0.0"
    ]
    assert _problems(points_at="d" * 40) == [
        f"v7.1.0 points at {'d' * 40}, not {COMMIT}"
    ]
