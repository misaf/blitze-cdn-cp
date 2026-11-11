"""The rendered edge configuration of one release, as an addressable value.

A bundle is laid out exactly as the edge's candidate directory
`/etc/blitzecdn/releases/<release digest>/`, paths relative to it; links keep
their absolute targets. Its identity is the digest of its manifest, in the
format the edge seals a candidate with.
"""

from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath

__all__ = [
    "ConfigurationBundle",
    "ConfigurationChange",
    "ConfigurationChangeKind",
    "ConfigurationDiff",
]


@dataclass(frozen=True, slots=True)
class ConfigurationBundle:
    """Files and links by path relative to the release's candidate directory."""

    files: Mapping[str, bytes] = field(default_factory=dict)
    links: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for path in (*self.files, *self.links):
            relative = PurePosixPath(path)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValueError(f"bundle path {path!r} is not inside the candidate")
        overlap = set(self.files) & set(self.links)
        if overlap:
            raise ValueError(
                f"bundle paths are both files and links: {sorted(overlap)}"
            )
        if "nginx.conf" not in self.files:
            raise ValueError("a configuration bundle has no nginx.conf")

    def manifest(self) -> dict[str, str]:
        """Content hash per file and `link:<target>` per link, as the edge seals."""
        entries = {
            path: hashlib.sha256(content).hexdigest()
            for path, content in self.files.items()
        }
        entries.update({path: "link:" + target for path, target in self.links.items()})
        return dict(sorted(entries.items()))

    @property
    def digest(self) -> str:
        """The bundle's identity: the digest of its canonical manifest."""
        encoded = json.dumps(self.manifest(), sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def encode(self) -> str:
        """The bundle as one JSON document, carrying the manifest it is verified by."""
        return json.dumps(
            {
                "files": {
                    path: base64.b64encode(content).decode("ascii")
                    for path, content in sorted(self.files.items())
                },
                "links": dict(sorted(self.links.items())),
                "manifest": self.manifest(),
            },
            sort_keys=True,
        )

    @classmethod
    def decode(cls, document: str) -> ConfigurationBundle:
        """Read an encoded bundle, refusing one whose content fails its manifest."""
        try:
            data = json.loads(document)
            bundle = cls(
                files={
                    str(path): base64.b64decode(content, validate=True)
                    for path, content in data["files"].items()
                },
                links={
                    str(path): str(target) for path, target in data["links"].items()
                },
            )
        except (ValueError, KeyError, TypeError, AttributeError, binascii.Error) as exc:
            raise ValueError(f"configuration bundle is malformed: {exc}") from exc
        if bundle.manifest() != data["manifest"]:
            raise ValueError("configuration bundle content does not match its manifest")
        return bundle


class ConfigurationChangeKind(StrEnum):
    """How one path differs between two rendered trees."""

    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"


@dataclass(frozen=True, slots=True)
class ConfigurationChange:
    """One path that differs between two bundles, with its unified diff.

    A link reads as the line ``-> <target>``, so a file replaced by a link (or a
    retargeted link) diffs like any edit. ``diff`` is ``None`` when either side
    is not UTF-8 text; the change is still reported, by path.
    """

    path: str
    change: ConfigurationChangeKind
    diff: str | None


@dataclass(frozen=True, slots=True)
class ConfigurationDiff:
    """What changes on an edge going from ``before`` to ``after``, path by path.

    ``before`` is ``None`` when nothing was there yet, so every path is added.
    Only paths that differ are listed, sorted; unchanged ones are left out.
    """

    before: str | None
    after: str
    changes: tuple[ConfigurationChange, ...]

    @classmethod
    def between(
        cls, before: ConfigurationBundle | None, after: ConfigurationBundle
    ) -> ConfigurationDiff:
        """Compare two rendered trees by content, never by their digests alone."""
        old = _entries(before) if before is not None else {}
        new = _entries(after)
        changes: list[ConfigurationChange] = []
        for path in sorted(old.keys() | new.keys()):
            previous, current = old.get(path), new.get(path)
            if previous == current:
                continue
            if previous is None:
                kind = ConfigurationChangeKind.ADDED
            elif current is None:
                kind = ConfigurationChangeKind.REMOVED
            else:
                kind = ConfigurationChangeKind.MODIFIED
            changes.append(
                ConfigurationChange(
                    path=path, change=kind, diff=_unified(path, previous, current)
                )
            )
        return cls(
            before=before.digest if before is not None else None,
            after=after.digest,
            changes=tuple(changes),
        )

    @property
    def changed(self) -> bool:
        """Whether deploying ``after`` over ``before`` changes anything at all."""
        return bool(self.changes)

    def summary(self) -> dict[str, object]:
        """The diff as a JSON-ready document, for a surface to add its ids to."""
        return {
            "changed": self.changed,
            "before": self.before,
            "after": self.after,
            "changes": [
                {
                    "path": change.path,
                    "change": change.change.value,
                    "diff": change.diff,
                }
                for change in self.changes
            ],
        }

    def patch(self) -> str:
        """Every text change as one unified diff, ready for ``patch`` or a pager."""
        return "".join(change.diff for change in self.changes if change.diff)


def _entries(bundle: ConfigurationBundle) -> dict[str, bytes]:
    """Each path's content as bytes, a link standing in as its target line."""
    entries = dict(bundle.files)
    entries.update(
        {path: f"-> {target}\n".encode() for path, target in bundle.links.items()}
    )
    return entries


def _unified(path: str, before: bytes | None, after: bytes | None) -> str | None:
    """The unified diff of one path, or ``None`` if either side is not text."""
    try:
        old = before.decode("utf-8") if before is not None else ""
        new = after.decode("utf-8") if after is not None else ""
    except UnicodeDecodeError:
        return None
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile="/dev/null" if before is None else f"a/{path}",
        tofile="/dev/null" if after is None else f"b/{path}",
    )
    # A last line without a newline would run into the next file's header.
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)
