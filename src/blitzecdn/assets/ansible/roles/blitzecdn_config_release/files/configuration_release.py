"""Edge-local durable configuration transaction; standard library only.

`stage` writes the controller's bundle as the candidate tree and refuses it
unless it matches the controller's manifest. The journal is written before the
pointer switches, so replaying after a crash keeps the original rollback
target. Nginx mounts the parent, so a swapped link is live immediately.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

#: Written beside the staged tree by the edge, never shipped in a bundle.
RUNTIME_IMAGE = "runtime-image"


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
        self.staged = root / f".{digest}.staged"
        self.records = records

    def inventory(self) -> dict[str, str]:
        """Hash bytes and link targets, refusing links that leave this tree."""
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

    def stage(self, bundle: Path) -> None:
        """Write the controller's bundle as this release's candidate tree.

        An unsealed candidate is replaced; a sealed one must be this bundle. Either
        way the tree must match the manifest exactly.
        """
        files, links, manifest = read_bundle(bundle)
        # A retained old release may be staged again before activation. Protect
        # it across helper invocations, even if another commit runs in between.
        atomic_write(self.staged, b"")
        if self.seal.exists():
            self.verify()
        else:
            if self.candidate.exists() or self.candidate.is_symlink():
                shutil.rmtree(self.candidate)
            self.candidate.mkdir(mode=0o755)
            for name, content in files.items():
                path = self.candidate / name
                path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                path.write_bytes(content)
                path.chmod(0o644)
            for name, target in links.items():
                path = self.candidate / name
                path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                os.symlink(target, path)
        staged = self.inventory()
        staged.pop(RUNTIME_IMAGE, None)
        if staged != manifest:
            raise ValueError("staged configuration does not match the release bundle")
        bundle.unlink()

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
            self.staged.unlink(missing_ok=True)
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
        self.staged.unlink(missing_ok=True)
        sync_directory(self.root)

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

    def commit(self, keep_older: int = 2) -> None:
        if keep_older < 0:
            raise ValueError("older release retention must not be negative")
        self.verify()
        if self.pointer() != self.digest:
            raise ValueError("cannot commit a release that is not active")
        data = self.read_journal()
        if data["status"] == "rolled_back":
            raise ValueError("cannot commit a withdrawn activation")
        if data["status"] != "committed":
            data["status"] = "committed"
            self.save_journal(data)
        # The serving transaction is already durable. A collection failure must
        # not send the controller down the failed-activation rollback path.
        try:
            self.prune(keep_older)
        except (OSError, ValueError) as exc:
            print(f"configuration release cleanup deferred: {exc}", file=sys.stderr)

    def prune(self, keep_older: int) -> None:
        """Collect completed transactions; callers hold the edge mutation lock.

        Unjournaled trees and staging markers are deliberately left alone: they
        may belong to a deployment between stage and activate. Pending journals
        protect both sides of recovery. Completed journals do not pin their
        entire predecessor chain.
        """
        current = self.pointer()
        protected = {current}
        completed = []
        # Validate all managed journals before deleting anything. Corrupt state
        # must defer collection, rather than hide an unknown recovery target.
        for journal in sorted(self.root.glob(".*.transaction.json")):
            match = re.fullmatch(r"\.([0-9a-f]{64})\.transaction\.json", journal.name)
            if match is None:
                continue
            if journal.is_symlink():
                raise ValueError(f"transaction journal is a symbolic link: {journal}")
            data = json.loads(journal.read_text())
            if not isinstance(data, dict):
                raise ValueError(f"invalid transaction journal: {journal}")
            status, previous = data.get("status"), data.get("previous")
            if (
                status not in ("pending", "committed", "rolled_back")
                or "previous" not in data
                or (
                    previous is not None
                    and (
                        not isinstance(previous, str)
                        or re.fullmatch(r"[0-9a-f]{64}", previous) is None
                    )
                )
            ):
                raise ValueError(f"invalid transaction journal: {journal}")
            digest = match[1]
            if (
                digest == current
                or status == "pending"
                or (status == "rolled_back" and current == previous)
            ):
                protected.update((digest, previous))
            if status != "pending":
                completed.append((journal.stat().st_mtime_ns, digest, journal))
        protected.update(
            match[1]
            for path in self.root.glob(".*.staged")
            if (match := re.fullmatch(r"\.([0-9a-f]{64})\.staged", path.name))
        )
        older = sorted(
            (entry for entry in completed if entry[1] not in protected), reverse=True
        )
        for _, digest, journal in older[keep_older:]:
            tree = self.root / digest
            if tree.is_symlink():
                raise ValueError(f"release tree is a symbolic link: {tree}")
            # Tree first: after a crash the journal still identifies an orphan
            # for the next commit to collect. rmtree is safe to retry halfway.
            if tree.exists():
                shutil.rmtree(tree)
            journal.unlink()
            sync_directory(self.root)


def read_bundle(
    path: Path,
) -> tuple[dict[str, bytes], dict[str, str], dict[str, str]]:
    """The bundle's files, links and manifest, checked against each other."""
    try:
        data = json.loads(path.read_text())
        files = {
            name: base64.b64decode(content, validate=True)
            for name, content in data["files"].items()
        }
        links = {name: str(target) for name, target in data["links"].items()}
        manifest = {name: str(value) for name, value in data["manifest"].items()}
    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error) as exc:
        raise ValueError(f"configuration bundle is malformed: {exc}") from exc
    for name in (*files, *links):
        relative = PurePosixPath(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or name in ("manifest.json", RUNTIME_IMAGE)
        ):
            raise ValueError(f"bundle path {name!r} is not a candidate entry")
    computed = {
        name: hashlib.sha256(content).hexdigest() for name, content in files.items()
    }
    computed.update({name: "link:" + target for name, target in links.items()})
    if computed != manifest:
        raise ValueError("configuration bundle content does not match its manifest")
    return files, links, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=("stage", "seal", "verify", "activate", "rollback", "commit"),
    )
    parser.add_argument("root", type=Path)
    parser.add_argument("digest")
    parser.add_argument("records", nargs="*", type=Path)
    parser.add_argument("--keep-older", type=int, default=2)
    args = parser.parse_args()
    if args.keep_older < 0:
        parser.error("--keep-older must not be negative")
    release = ConfigurationRelease(args.root, args.digest, tuple(args.records))
    operations: dict[str, Callable[[], None]] = {
        "stage": lambda: release.stage(args.records[0]),
        "seal": release.seal_candidate,
        "verify": release.verify,
        "activate": release.activate,
        "rollback": release.rollback,
        "commit": lambda: release.commit(args.keep_older),
    }
    if args.operation == "verify":
        release.verify()
    else:
        # All deployed mutations share one lock, including stage and seal.
        # Keep the lock file: unlinking it can split waiters across two inodes.
        with (args.root / ".mutation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            operations[args.operation]()


if __name__ == "__main__":
    main()
