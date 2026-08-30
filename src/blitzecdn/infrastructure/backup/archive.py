"""The archive container, and the private directory backups are built in.

One format for every backup — gzipped tar — chosen because it is the format an
operator already has a tool for on a host where BlitzeCDN will not start. A
backup that can only be read by the thing that is broken is not a backup.
"""

from __future__ import annotations

import os
import tarfile
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree

from blitzecdn.domain.backup import unsafe_member
from blitzecdn.exceptions import ConfigurationError

#: Refuse a manifest larger than this rather than read it into memory. The real
#: one is a few hundred bytes; anything approaching this is not a manifest.
_MAX_MEMBER_BYTES = 1_048_576

#: Members are not a stream we consume lazily — the whole list is checked
#: before anything is extracted — so a cap keeps a hostile archive from
#: exhausting memory during the check that was supposed to protect us.
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
        """Pack ``source``'s contents into a new archive at ``destination``.

        Built beside the destination and linked into place, so a reader never
        sees a half-written archive and a crash leaves a temporary file rather
        than a truncated backup. ``os.link`` rather than ``os.replace`` is what
        makes "never overwrite" true against a concurrent writer: replace would
        happily clobber a file that appeared after the earlier existence check.
        """
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent
        )
        temporary = Path(temporary_name)
        try:
            # The archive carries private keys. It is created 0600 before a
            # byte is written, not chmod-ed afterwards, so there is no window
            # in which it is readable.
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
        """Add one staged file, with nothing of this host recorded on it.

        Ownership and the absolute path are deliberately dropped. A backup is
        restored onto a host whose uids differ and whose install directory may
        differ, and a tar that remembers `blitzecdn:1001` and
        `/opt/blitzecdn/.state` would restore the wrong thing on it.
        """
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
        """Unpack a *previously validated* archive into a private directory.

        The member rules are applied again here rather than trusted from the
        caller. They are cheap, and this is the call that writes: a future
        refactor that reorders the service must not be able to turn a check
        that ran into a check that did not.
        """
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
            # `filter="data"` is the standard library's own hardening — it
            # strips ownership and refuses escapes and special files. Belt and
            # braces with the rules above, which are the ones this project
            # states and tests.
            handle.extractall(destination, members=members, filter="data")

    @staticmethod
    @contextmanager
    def _open(archive: Path) -> Iterator[tarfile.TarFile]:
        try:
            # This *is* the context manager; opening inside the `try`
            # is what turns "not a tar file" into an operator-safe message
            # rather than a traceback out of the standard library.
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
    """Somewhere private to stage a backup, removed however the caller leaves.

    Created under the state directory rather than `/tmp`: a staged backup is a
    complete copy of the database and every private key, and `/tmp` is
    world-readable, frequently a tmpfs too small to hold it, and on many hosts
    shared with every other service.
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
