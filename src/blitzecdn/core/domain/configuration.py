"""The rendered edge configuration of one release, as an addressable value.

A bundle is the tree an edge serves from: the Nginx entrypoint, its includes,
the sites, the module list and executable njs resources, laid out exactly as
the edge's candidate directory `/etc/blitzecdn/releases/<release digest>/`.
Paths are relative to that directory. Links keep their absolute targets,
because that is what the edge creates and what its seal records.

The release digest is rendered into the configuration — the edge reports it
on every response so the controller can verify activation — so a bundle is a
projection of a release rather than part of it. Its own identity is the digest
of its manifest, which uses the format the edge seals a candidate with.
"""

from __future__ import annotations

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
