"""The rendered edge configuration of one release, as an addressable value.

A bundle is laid out exactly as the edge's candidate directory
`/etc/blitzecdn/releases/<release digest>/`, paths relative to it; links keep
their absolute targets. Its identity is the digest of its manifest, in the
format the edge seals a candidate with.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath

__all__ = ["ConfigurationBundle"]


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
