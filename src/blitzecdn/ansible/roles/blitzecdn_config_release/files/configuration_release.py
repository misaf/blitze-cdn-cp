"""Edge-local durable configuration transaction; Python's standard library only.

The journal is written before switching the pointer. Replaying activation after
a controller crash therefore preserves the original rollback target, including
the Compose definition and image record. The parent directory is mounted into
Nginx, so replacing a link is visible to an already running container.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


def atomic_write(path: Path, content: bytes) -> None:
    """Replace and persist a file, including the containing directory entry."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ConfigurationRelease:
    """One release's sealed tree and its recoverable activation journal."""

    def __init__(self, root: Path, digest: str, records: tuple[Path, ...]) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(
                "release digest must be 64 lowercase hexadecimal characters"
            )
        self.root = root
        self.digest = digest
        self.candidate = root / digest
        self.seal = self.candidate / "manifest.json"
        self.current = root / "current"
        self.journal = root / f".{digest}.transaction.json"
        self.records = records

    def inventory(self) -> dict[str, str]:
        """Hash bytes and link destinations; reject links outside this tree.

        Directories themselves carry no serving content. Link targets must be
        inside the candidate so a sealed site's include cannot drift through
        another release's mutable pointer.
        """
        entries = {}
        for path in sorted(self.candidate.rglob("*")):
            if path == self.seal:
                continue
            if path.is_symlink():
                path.resolve(strict=True).relative_to(self.candidate.resolve())
                entries[str(path.relative_to(self.candidate))] = "link:" + os.readlink(
                    path
                )
            elif path.is_file():
                entries[str(path.relative_to(self.candidate))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
            elif not path.is_dir():
                raise ValueError(f"unsupported candidate entry: {path}")
        if "nginx.conf" not in entries:
            raise ValueError("candidate has no nginx.conf")
        return entries

    def verify(self) -> None:
        if json.loads(self.seal.read_text()) != self.inventory():
            raise ValueError("sealed configuration content changed")

    def seal_candidate(self) -> None:
        if self.seal.exists():
            self.verify()
        else:
            # The seal is a durability boundary: flush resources and their
            # directory entries before publishing a manifest that names them.
            for path in sorted(self.candidate.rglob("*"), reverse=True):
                if path.is_symlink():
                    continue
                if path.is_dir():
                    sync_directory(path)
                elif path.is_file():
                    descriptor = os.open(path, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            atomic_write(
                self.seal, json.dumps(self.inventory(), sort_keys=True).encode()
            )
            sync_directory(self.root)

    def pointer(self) -> str | None:
        if self.current.is_symlink():
            target = os.readlink(self.current)
            if re.fullmatch(r"[0-9a-f]{64}", target) is None:
                raise ValueError("active pointer is not a managed release")
            return target
        if self.current.exists():
            raise ValueError("active pointer must be a symbolic link")
        return None

    def switch(self, digest: str | None) -> None:
        if digest is None:
            self.current.unlink(missing_ok=True)
        else:
            # The temporary name is unique; a crash can leave an unused link,
            # never a missing current link between unlink and symlink.
            descriptor, temporary = tempfile.mkstemp(dir=self.root)
            os.close(descriptor)
            os.unlink(temporary)
            try:
                os.symlink(digest, temporary)
                os.replace(temporary, self.current)
            finally:
                if os.path.lexists(temporary):
                    os.unlink(temporary)
        sync_directory(self.root)

    def read_journal(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.journal.read_text())
        return data

    def save_journal(self, data: dict[str, Any]) -> None:
        atomic_write(self.journal, json.dumps(data, sort_keys=True).encode())

    def activate(self) -> None:
        self.verify()
        current = self.pointer()
        data = self.read_journal() if self.journal.exists() else {}
        if data.get("status") == "pending":
            if current not in (self.digest, data["previous"]):
                raise ValueError("another release superseded this activation")
        elif current == self.digest:
            # Already committed: deploying the same release again must not
            # manufacture a rollback transaction pointing at itself.
            return
        else:
            data = {
                "status": "pending",
                "previous": current,
                "records": {
                    str(path): base64.b64encode(path.read_bytes()).decode()
                    if path.exists()
                    else None
                    for path in self.records
                },
            }
            self.save_journal(data)
        self.switch(self.digest)

    def rollback(self) -> None:
        if not self.journal.exists():
            # Failure before activation has no side effects to withdraw.
            return
        data = self.read_journal()
        if data["status"] == "committed":
            raise ValueError("cannot withdraw a committed configuration transaction")
        if self.pointer() not in (self.digest, data["previous"]):
            raise ValueError("another release superseded this activation")
        previous = data["previous"]
        if previous is not None:
            ConfigurationRelease(self.root, previous, ()).verify()
        self.switch(previous)
        for name, encoded in data["records"].items():
            path = Path(name)
            if encoded is None:
                path.unlink(missing_ok=True)
                sync_directory(path.parent)
            else:
                atomic_write(path, base64.b64decode(encoded))
        data["status"] = "rolled_back"
        self.save_journal(data)

    def commit(self) -> None:
        self.verify()
        if self.pointer() != self.digest:
            raise ValueError("cannot commit a release that is not active")
        data = self.read_journal()
        if data["status"] == "rolled_back":
            raise ValueError("cannot commit a withdrawn activation")
        data["status"] = "committed"
        self.save_journal(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=("seal", "verify", "activate", "rollback", "commit")
    )
    parser.add_argument("root", type=Path)
    parser.add_argument("digest")
    parser.add_argument("records", nargs="*", type=Path)
    args = parser.parse_args()
    release = ConfigurationRelease(args.root, args.digest, tuple(args.records))
    operations = {
        "seal": release.seal_candidate,
        "verify": release.verify,
        "activate": release.activate,
        "rollback": release.rollback,
        "commit": release.commit,
    }
    operations[args.operation]()


if __name__ == "__main__":
    main()
