"""The archive container — gzipped tar, readable where BlitzeCDN won't start."""

from __future__ import annotations

import os
import tarfile
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree

from blitzecdn.capabilities.backup.domain import unsafe_member
from blitzecdn.core.exceptions import ConfigurationError

#: Refuse a manifest larger than this rather than read it into memory. The real
#: one is a few hundred bytes; anything approaching this is not a manifest.
_MAX_MEMBER_BYTES = 1_048_576

#: The whole member list is checked before extraction, so cap it.
_MAX_MEMBERS = 100_000


@dataclass(frozen=True)
class TarEntry:
    """One tar member, reduced to what the safety rules need."""

    name: str
    is_link: bool
    link_target: str | None


class TarArchive:
    """Gzipped tar, written atomically and read only after it is checked."""

    def write(self, source: Path, destination: Path) -> None:
        """Pack ``source`` into a new archive at ``destination``, never overwriting.

        Built beside it and hard-linked into place, so readers never see a partial file
        and a concurrent writer's file is never clobbered.
        """
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent
        )
        temporary = Path(temporary_name)
        try:
            # Created 0600 before any byte is written; it carries private keys.
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                    for path in sorted(source.rglob("*")):
                        self._add(archive, path, path.relative_to(source).as_posix())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise ConfigurationError(
                    f"{destination} already exists; refusing to overwrite a backup"
                ) from exc
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _add(archive: tarfile.TarFile, path: Path, name: str) -> None:
        """Add one staged file without this host's ownership or absolute path."""
        if path.is_symlink():
            raise ConfigurationError(f"refusing to archive a symlink: {name}")
        if not (path.is_file() or path.is_dir()):
            raise ConfigurationError(f"refusing to archive a special file: {name}")
        info = archive.gettarinfo(str(path), arcname=name)
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mode = 0o700 if path.is_dir() else 0o600
        if info.isdir():
            archive.addfile(info)
            return
        with path.open("rb") as content:
            archive.addfile(info, content)

    def entries(self, archive: Path) -> Sequence[TarEntry]:
        """List an archive's members without extracting any of them."""
        found: list[TarEntry] = []
        names: set[str] = set()
        with self._open(archive) as handle:
            for member in handle:
                if len(found) >= _MAX_MEMBERS:
                    raise ConfigurationError(
                        f"{archive} holds more than {_MAX_MEMBERS} entries"
                    )
                if not (
                    member.isfile()
                    or member.isdir()
                    or member.islnk()
                    or member.issym()
                ):
                    raise ConfigurationError(
                        f"archive contains a special file, which is never "
                        f"restored: {member.name}"
                    )
                if member.name in names:
                    raise ConfigurationError(
                        f"archive contains a duplicate entry: {member.name}"
                    )
                names.add(member.name)
                found.append(
                    TarEntry(
                        name=member.name,
                        is_link=member.islnk() or member.issym(),
                        link_target=member.linkname or None,
                    )
                )
        return tuple(found)

    def read_member(self, archive: Path, name: str) -> bytes:
        with self._open(archive) as handle:
            try:
                member = handle.getmember(name)
            except KeyError as exc:
                raise ConfigurationError(f"{archive} has no {name}") from exc
            if member.size > _MAX_MEMBER_BYTES:
                raise ConfigurationError(f"{name} is implausibly large")
            stream = handle.extractfile(member)
            if stream is None:
                raise ConfigurationError(f"{name} is not a readable file")
            with stream:
                return stream.read(_MAX_MEMBER_BYTES + 1)[:_MAX_MEMBER_BYTES]

    def extract(self, archive: Path, destination: Path) -> None:
        """Unpack a validated archive, re-checking every member as it writes."""
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._open(archive) as handle:
            members = handle.getmembers()
            for member in members:
                reason = unsafe_member(
                    member.name,
                    is_link=member.islnk() or member.issym(),
                    link_target=member.linkname or None,
                )
                if reason is not None:
                    raise ConfigurationError(reason)
            # `filter="data"` adds the standard library's hardening to our own rules.
            handle.extractall(destination, members=members, filter="data")

    @staticmethod
    @contextmanager
    def _open(archive: Path) -> Iterator[tarfile.TarFile]:
        try:
            # Opened inside the `try` so "not a tar file" becomes a clear message.
            handle = tarfile.open(archive, mode="r:gz")  # noqa: SIM115
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise ConfigurationError(f"{archive} is not a readable backup") from exc
        try:
            yield handle
        except tarfile.TarError as exc:
            raise ConfigurationError(f"{archive} is damaged: {exc}") from exc
        finally:
            handle.close()


class TemporaryWorkspace:
    """Private staging for a backup, removed however the caller leaves.

    Under the state directory, not `/tmp`: it holds the database and every key.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @contextmanager
    def scratch(self, prefix: str) -> Iterator[Path]:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._root.chmod(0o700)
        directory = Path(tempfile.mkdtemp(prefix=prefix, dir=self._root))
        try:
            yield directory
        finally:
            rmtree(directory, ignore_errors=True)


__all__ = ["TarArchive", "TarEntry", "TemporaryWorkspace"]
