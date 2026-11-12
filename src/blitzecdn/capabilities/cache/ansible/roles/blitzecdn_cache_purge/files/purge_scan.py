#!/usr/bin/env python3
"""Remove every cached response with a key, under a prefix, or carrying a tag.

Standalone standard-library code: Ansible copies this to an edge, where the
control-plane package is not installed. It reads a JSON request on stdin and
writes a JSON report on stdout.

nginx open source cannot delete by pattern, and a cache file's name is the MD5
of its key, so what a file holds has to be read back from it. nginx writes a
cache file as a binary header, a ``KEY: <key>`` line, then the upstream
response exactly as the origin sent it: status line, header lines, a blank
line, the body.

The index
    Reading the head of every cached file on every purge makes a purge cost
    the size of the cache. Instead, what each file says (its key and its tags)
    is kept in a SQLite index on the edge, and a purge resolves keys, prefixes
    and tags through it.

    The index is brought up to date at the start of every purge, and only
    where the cache changed. nginx stores an entry by renaming a finished
    temporary file into place and removes one by unlinking it; both change
    the mtime of the directory holding it. A directory whose mtime is the one
    the index recorded therefore holds the files the index lists, and is not
    even listed. A directory that changed is listed, and of its files only the
    ones the index has not seen (by inode, size and mtime) are read. A purge
    on a quiet edge costs one ``stat`` per cache directory; on a busy one it
    adds the new entries since the last purge.

    nginx rewrites a file in place only to refresh the validity fields of its
    binary header after revalidation, never its key or response header, and
    that changes the file's mtime anyway, so the file is read again.

    Timestamps are coarser than the changes they record. A directory whose
    mtime is within ``SETTLE_NS`` of the clock when it is listed is stored as
    unsettled and listed again next time, so a store that lands in the same
    tick as the listing cannot hide behind an unchanged mtime.

    The index is derived data. A missing, corrupt or foreign index (another
    cache directory, device, tag header or schema) is discarded and rebuilt
    by reading every file once, which is what every purge used to do.

Keys
    A named purge gives the exact keys of a URL: one per scheme, listener
    port, method and encoding. Hashing a key gives the file of the entry it
    names, but not every file holding that key. When the origin answers with
    ``Vary``, nginx files the first representation under the MD5 of the key
    and every other one under a hash of the key plus the varied request
    headers, and it reopens such a secondary file whenever the request's
    headers select it. Every one of those files carries the same KEY line, so
    the index maps the key to all of them.

Prefixes
    The key is built by blitzecdn_nginx as

        $scheme $server_port $request_method $host <request target> <encoding>

    with nothing between the parts. The scheme is lowercase letters, the port
    digits and the method uppercase letters, and nginx lowercases ``$host``,
    so the hostname starts at the first character after the method. The index
    keeps the key from there on, and a prefix is a range over it. Every
    request target begins with ``/``, which is where the hostname ends:
    ``example.com/`` cannot match a key for ``example.com.evil/``.

Tags
    The origin names an entry's tags in a response header, ``Cache-Tag:
    product-42, listing`` by default. nginx stores that header with the entry
    even though the edge hides it from visitors. The index records the tags
    of each file it reads, and forgets them when the file goes.

    Header names match case-insensitively; the header may appear more than
    once; values are split on commas and trimmed. Tags are case-insensitive:
    ``Product-42`` and ``product-42`` are one tag. A purge would rather remove
    an entry it was not sure about than leave one serving stale content.

Files that cannot be decided
    A tag purge needs the whole response header. nginx refuses an upstream
    header larger than ``proxy_buffer_size`` (a page or two by default), so
    the header ends well inside ``MAX_HEAD_BYTES``. If it does not, or a file
    has no KEY line, or it cannot be read, the file is counted as
    ``unchecked`` rather than as a non-match: the role fails the edge, so a
    purge cannot report success while an entry it should have removed is
    still being served.

While nginx keeps serving
    Nothing here pauses nginx. A purge removes every matching file already in
    place when its index refresh lists the file's directory. An entry nginx
    renames into place after that may survive until the next purge, and a
    file nginx evicts first is simply gone. docs/design/cache.md
    ("Concurrent cache activity") states the guarantee, and
    tests/capabilities/cache/test_cache_purge_nginx.py checks it against a
    real nginx.

Emptying the cache
    ``"all": true`` removes every regular file under the cache directory,
    temporary files included, and empties the index, reporting how many went.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

#: The first read: nginx's binary header, a typical key and response header.
HEAD_BYTES = 8192
#: How far a read goes looking for the end of the response header before
#: giving up on the file. Far past any proxy_buffer_size nginx would accept a
#: header into, so reaching it means the file is not what the scan expects.
MAX_HEAD_BYTES = 65536
KEY_MARKER = b"\nKEY: "
#: The scheme, listener port and method that lead every key.
KEY_LEAD = re.compile(rb"^(?:https?)[0-9]+[A-Z]+")
#: The blank line ending the response header. nginx accepts bare LF too.
HEADER_END = re.compile(rb"\r?\n\r?\n")
#: Files nginx renames into place are named by the full MD5. Anything else,
#: such as a temporary file still being written, is left alone.
CACHE_FILE = re.compile(r"^[0-9a-f]{32}$")
DEFAULT_TAG_HEADER = "Cache-Tag"
#: An index that lives only as long as the purge: every file is read.
NO_INDEX = ":memory:"
#: Bumped whenever the tables change; an index with another version is rebuilt.
SCHEMA_VERSION = 1
#: How long a directory's mtime has to be in the past before the index trusts
#: it. Linux stamps mtimes from a clock that ticks every few milliseconds.
SETTLE_NS = 2_000_000_000

SCHEMA = """
CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Every directory under the cache, relative to it ('' is the cache itself).
-- mtime_ns is NULL until the directory is settled: it is listed every time.
CREATE TABLE dirs (
    path TEXT PRIMARY KEY,
    parent TEXT,
    mtime_ns INTEGER
);
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    dir TEXT NOT NULL,
    name TEXT NOT NULL,
    ino INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    -- NULL when the file has no KEY line.
    key BLOB,
    -- 0 when the key ran past the read, so key is only its start.
    whole_key INTEGER NOT NULL,
    -- The key from the hostname on, which is what a prefix names.
    target BLOB,
    -- 0 when the response header did not end within the read.
    header_read INTEGER NOT NULL,
    UNIQUE (dir, name)
);
CREATE INDEX files_key ON files (key);
CREATE INDEX files_target ON files (target);
CREATE TABLE tags (
    file INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    tag BLOB NOT NULL
);
CREATE INDEX tags_tag ON tags (tag);
CREATE INDEX tags_file ON tags (file);
"""


class Head(NamedTuple):
    """What the start of a cache file says about the entry it holds."""

    #: The key, or as much of it as was read; None if there is no KEY line.
    key: bytes | None
    #: Whether the read reached the end of the KEY line, so ``key`` is all of it.
    whole_key: bool
    #: The response header lines after the status line; None if the read
    #: ended before the blank line that closes them.
    header: bytes | None


def read_head(path: Path, *, need_header: bool) -> Head:
    """Read a cache file's key and, if asked, its whole response header.

    Raises OSError if the file cannot be read.
    """
    with path.open("rb") as handle:
        head = handle.read(HEAD_BYTES)
        while True:
            start = head.find(KEY_MARKER)
            key_end = head.find(b"\n", start + len(KEY_MARKER)) if start >= 0 else -1
            header_end = HEADER_END.search(head, key_end + 1) if key_end >= 0 else None
            done = (key_end >= 0 and not need_header) or header_end is not None
            if done or len(head) >= MAX_HEAD_BYTES:
                break
            more = handle.read(min(HEAD_BYTES, MAX_HEAD_BYTES - len(head)))
            if not more:
                break
            head += more
    if start < 0:
        return Head(None, False, None)
    start += len(KEY_MARKER)
    if key_end < 0:
        # The key runs past the read. Its start is all a prefix needs.
        return Head(head[start:], False, None)
    key = head[start:key_end]
    if header_end is None:
        return Head(key, True, None)
    response = head[key_end + 1 : header_end.start()]
    # Drop the status line; what is left is the header lines.
    _status, _, lines = response.partition(b"\n")
    return Head(key, True, lines)


def header_tags(lines: bytes, name: bytes) -> set[bytes]:
    """Every tag in every ``name`` header, lowercased, folded lines joined."""
    values: list[bytes] = []
    current: bytes | None = None
    for raw in lines.split(b"\n"):
        line = raw.rstrip(b"\r")
        if line[:1] in (b" ", b"\t"):
            # Obsolete line folding: a continuation of the previous header,
            # which only extends a tag value if that header was the tag one.
            if current == name:
                values[-1] += b" " + line.strip()
            continue
        field, colon, value = line.partition(b":")
        current = field.strip().lower() if colon else None
        if current == name:
            values.append(value.strip())
    return {
        tag.strip().lower()
        for value in values
        for tag in value.split(b",")
        if tag.strip()
    }


def key_target(key: bytes) -> bytes | None:
    """The key from the hostname on, or None if it does not lead as ours do."""
    lead = KEY_LEAD.match(key)
    return None if lead is None else key[lead.end() :]


def _after(prefix: bytes) -> bytes | None:
    """The least byte string greater than every string starting with ``prefix``."""
    stripped = prefix.rstrip(b"\xff")
    if not stripped:
        return None
    return stripped[:-1] + bytes([stripped[-1] + 1])


# ----------------------------------------------------------------------
# The index
# ----------------------------------------------------------------------


def _identity(root: Path, tag_header: bytes) -> str:
    """What an index describes; any change means it describes something else."""
    resolved = root.resolve()
    return json.dumps(
        [str(resolved), resolved.stat().st_dev, tag_header.decode("latin-1")]
    )


def _connect(location: str, identity: str) -> sqlite3.Connection:
    db = sqlite3.connect(location, timeout=300, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys = ON")
        # One purge at a time per edge: the second waits for the first's index.
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        current = version == SCHEMA_VERSION and db.execute(
            "SELECT value FROM meta WHERE name = 'identity'"
        ).fetchone() == (identity,)
        if not current:
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall():
                db.execute(f'DROP TABLE "{name}"')
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT INTO meta VALUES ('identity', ?)", (identity,))
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except BaseException:
        db.close()
        raise
    return db


#: SQLITE_CORRUPT and SQLITE_NOTADB, the primary result codes.
CORRUPT_CODES = frozenset({11, 26})


def _corrupt(error: sqlite3.DatabaseError) -> bool:
    """Whether ``error`` says the file is damaged or not a database at all."""
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in CORRUPT_CODES
    # Python before 3.11 does not expose the code; SQLite's messages are fixed.
    message = str(error).lower()
    return "not a database" in message or "malformed" in message


def open_index(location: str, root: Path, tag_header: bytes) -> sqlite3.Connection:
    """The index at ``location``, rebuilt empty if it is not one for this cache.

    The connection holds a write transaction; commit it when done.
    """
    identity = _identity(root, tag_header)
    if location == NO_INDEX:
        return _connect(location, identity)
    Path(location).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        return _connect(location, identity)
    except sqlite3.DatabaseError as error:
        # A lock held past the timeout by another purge is a DatabaseError
        # too, and that index is healthy and in use: deleting it would let
        # this purge run beside the other one. Only corruption is rebuilt.
        if not _corrupt(error):
            raise
        # Corrupt, or not a database at all. It is only ever derived from the
        # cache, so it is thrown away rather than repaired.
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(location + suffix).unlink(missing_ok=True)
        return _connect(location, identity)


class Refresh(NamedTuple):
    #: Cache files whose heads were read because the index had not seen them.
    read: int
    #: Cache files that could not be read, so the index does not describe them.
    failed: int


def _join(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def _store(
    db: sqlite3.Connection,
    directory: str,
    name: str,
    status: os.stat_result,
    head: Head,
    tag_header: bytes,
) -> None:
    db.execute("DELETE FROM files WHERE dir = ? AND name = ?", (directory, name))
    cursor = db.execute(
        "INSERT INTO files"
        " (dir, name, ino, size, mtime_ns, key, whole_key, target, header_read)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            directory,
            name,
            status.st_ino,
            status.st_size,
            status.st_mtime_ns,
            head.key,
            int(head.whole_key),
            None if head.key is None else key_target(head.key),
            int(head.header is not None),
        ),
    )
    if head.header is not None:
        db.executemany(
            "INSERT INTO tags (file, tag) VALUES (?, ?)",
            ((cursor.lastrowid, tag) for tag in header_tags(head.header, tag_header)),
        )


def _relist(
    db: sqlite3.Connection,
    root: Path,
    directory: str,
    device: int,
    tag_header: bytes,
) -> tuple[list[str], int, int]:
    """Bring one changed directory's rows up to date.

    Returns its subdirectories, how many files were read, and how many could
    not be.
    """
    known = {
        name: (ino, size, mtime_ns)
        for name, ino, size, mtime_ns in db.execute(
            "SELECT name, ino, size, mtime_ns FROM files WHERE dir = ?", (directory,)
        )
    }
    subdirectories: list[str] = []
    present: set[str] = set()
    read = failed = 0
    with os.scandir(root / directory if directory else root) as entries:
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    # Like find -xdev: a filesystem mounted under the cache is
                    # not the cache.
                    if entry.stat(follow_symlinks=False).st_dev == device:
                        subdirectories.append(_join(directory, entry.name))
                    continue
                if not CACHE_FILE.match(entry.name):
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                status = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            present.add(entry.name)
            if known.get(entry.name) == (
                status.st_ino,
                status.st_size,
                status.st_mtime_ns,
            ):
                continue
            try:
                head = read_head(Path(entry.path), need_header=True)
            except FileNotFoundError:
                # Evicted by nginx between the listing and the read.
                present.discard(entry.name)
                continue
            except OSError:
                present.discard(entry.name)
                failed += 1
                continue
            read += 1
            _store(db, directory, entry.name, status, head, tag_header)
    db.executemany(
        "DELETE FROM files WHERE dir = ? AND name = ?",
        ((directory, name) for name in known.keys() - present),
    )
    return subdirectories, read, failed


def refresh(db: sqlite3.Connection, root: Path, tag_header: bytes) -> Refresh:
    """Make the index describe the cache as it is now, reading only what changed."""
    device = root.stat().st_dev
    recorded: dict[str, int | None] = {}
    children: dict[str, list[str]] = defaultdict(list)
    for path, parent, mtime_ns in db.execute("SELECT path, parent, mtime_ns FROM dirs"):
        recorded[path] = mtime_ns
        if parent is not None:
            children[parent].append(path)
    seen: set[str] = set()
    read = failed = 0
    pending: list[tuple[str, str | None]] = [("", None)]
    while pending:
        directory, parent = pending.pop()
        try:
            # The cache itself may be reached through a link; nothing under it is.
            status = (root / directory).lstat() if directory else root.stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(status.st_mode) or status.st_dev != device:
            continue
        seen.add(directory)
        mtime_ns = recorded.get(directory)
        if mtime_ns is not None and mtime_ns == status.st_mtime_ns:
            pending.extend((child, directory) for child in children[directory])
            continue
        settled = time.time_ns() - status.st_mtime_ns > SETTLE_NS
        try:
            subdirectories, dir_read, dir_failed = _relist(
                db, root, directory, device, tag_header
            )
        except FileNotFoundError:
            seen.discard(directory)
            continue
        read += dir_read
        failed += dir_failed
        # A file that could not be read is not in the index, so the directory
        # has to be listed again next time to find it.
        trusted = status.st_mtime_ns if settled and not dir_failed else None
        db.execute(
            "INSERT INTO dirs (path, parent, mtime_ns) VALUES (?, ?, ?)"
            " ON CONFLICT (path) DO UPDATE SET"
            " parent = excluded.parent, mtime_ns = excluded.mtime_ns",
            (directory, parent, trusted),
        )
        pending.extend((child, directory) for child in subdirectories)
    gone = [(path,) for path in recorded.keys() - seen]
    db.executemany("DELETE FROM files WHERE dir = ?", gone)
    db.executemany("DELETE FROM dirs WHERE path = ?", gone)
    return Refresh(read, failed)


# ----------------------------------------------------------------------
# Purging
# ----------------------------------------------------------------------


def _wanted(db: sqlite3.Connection, kind: str, values: Iterable[bytes]) -> None:
    """Load what a purge asks for into a table, so the index can be joined to it."""
    db.execute(
        "CREATE TEMP TABLE IF NOT EXISTS wanted"
        " (kind TEXT NOT NULL, value BLOB NOT NULL, PRIMARY KEY (kind, value))"
    )
    db.execute("DELETE FROM wanted WHERE kind = ?", (kind,))
    db.executemany(
        "INSERT OR IGNORE INTO wanted (kind, value) VALUES (?, ?)",
        ((kind, value) for value in values),
    )


def _matches(
    db: sqlite3.Connection,
    *,
    keys: frozenset[bytes],
    prefixes: tuple[bytes, ...],
    tags: frozenset[bytes],
) -> Iterator[tuple[int, str, str]]:
    if keys:
        _wanted(db, "key", keys)
        yield from db.execute(
            "SELECT f.id, f.dir, f.name FROM wanted w"
            " JOIN files f ON f.key = w.value"
            " WHERE w.kind = 'key' AND f.whole_key = 1"
        )
    for prefix in prefixes:
        upper = _after(prefix)
        if upper is None:
            yield from db.execute(
                "SELECT id, dir, name FROM files WHERE target >= ?", (prefix,)
            )
        else:
            yield from db.execute(
                "SELECT id, dir, name FROM files WHERE target >= ? AND target < ?",
                (prefix, upper),
            )
    if tags:
        _wanted(db, "tag", tags)
        yield from db.execute(
            "SELECT f.id, f.dir, f.name FROM wanted w"
            " JOIN tags t ON t.tag = w.value"
            " JOIN files f ON f.id = t.file"
            " WHERE w.kind = 'tag'"
        )


def _undecided(
    db: sqlite3.Connection,
    matched: set[int],
    *,
    keys: frozenset[bytes],
    tags: frozenset[bytes],
) -> int:
    """Files that might have matched but the index cannot say."""
    undecided = 0
    # No KEY line: nothing can be said about it at all.
    for (file_id,) in db.execute("SELECT id FROM files WHERE key IS NULL"):
        undecided += file_id not in matched
    # A key cut off where a requested key could still continue, or with no
    # response header read for a tag to be found in.
    for file_id, start in db.execute(
        "SELECT id, key FROM files WHERE key IS NOT NULL AND whole_key = 0"
    ):
        if file_id not in matched and (
            tags or any(key.startswith(start) for key in keys)
        ):
            undecided += 1
    if tags:
        for (file_id,) in db.execute(
            "SELECT id FROM files WHERE whole_key = 1 AND header_read = 0"
        ):
            undecided += file_id not in matched
    return undecided


def purge(
    root: Path,
    *,
    index: str = NO_INDEX,
    keys: frozenset[bytes] = frozenset(),
    prefixes: tuple[bytes, ...] = (),
    tags: frozenset[bytes] = frozenset(),
    tag_header: bytes = DEFAULT_TAG_HEADER.lower().encode(),
) -> dict[str, int]:
    """Delete what any key, prefix or tag matches, resolved through the index."""
    db = open_index(index, root, tag_header)
    try:
        refreshed = refresh(db, root, tag_header)
        matched: dict[int, tuple[str, str]] = {}
        for file_id, directory, name in _matches(
            db, keys=keys, prefixes=prefixes, tags=tags
        ):
            matched[file_id] = (directory, name)
        unchecked = refreshed.failed + _undecided(
            db, set(matched), keys=keys, tags=tags
        )
        indexed = db.execute("SELECT count(*) FROM files").fetchone()[0]
        removed = 0
        forgotten: list[tuple[int]] = []
        for file_id, (directory, name) in matched.items():
            try:
                (root / directory / name).unlink()
            except FileNotFoundError:
                forgotten.append((file_id,))
                continue
            except OSError:
                # Still on disk and still served: the edge has to fail.
                unchecked += 1
                continue
            forgotten.append((file_id,))
            removed += 1
        db.executemany("DELETE FROM files WHERE id = ?", forgotten)
        db.execute("COMMIT")
    finally:
        db.close()
    return {
        "scanned": indexed + refreshed.failed,
        "read": refreshed.read,
        "removed": removed,
        "unchecked": unchecked,
    }


def empty(
    root: Path,
    *,
    index: str = NO_INDEX,
    tag_header: bytes = DEFAULT_TAG_HEADER.lower().encode(),
) -> dict[str, int]:
    """Delete every regular file under the cache, as ``find -xdev -type f -delete``.

    Files rather than the tree: the level directories are created by nginx at
    startup and removing them makes every subsequent store fail until a reload.
    """
    db = open_index(index, root, tag_header)
    try:
        device = root.stat().st_dev
        scanned = removed = unchecked = 0
        for directory, subdirectories, files in os.walk(root):
            subdirectories[:] = [
                name
                for name in subdirectories
                if (Path(directory) / name).lstat().st_dev == device
            ]
            for name in files:
                path = Path(directory) / name
                try:
                    if not stat.S_ISREG(path.lstat().st_mode):
                        continue
                except FileNotFoundError:
                    continue
                scanned += 1
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    unchecked += 1
                    continue
                removed += 1
        # Every directory changed, so the next purge would relist them all
        # anyway; dropping the rows saves it deleting each one.
        db.execute("DELETE FROM files")
        db.execute("DELETE FROM dirs")
        db.execute("COMMIT")
    finally:
        db.close()
    return {"scanned": scanned, "read": 0, "removed": removed, "unchecked": unchecked}


def main() -> int:
    # The index names every cached URL; it is root's alone, like the cache.
    os.umask(0o077)
    request: dict[str, Any] = json.load(sys.stdin)
    root = Path(request["path"])
    index = str(request.get("index") or NO_INDEX)
    keys = frozenset(key.encode() for key in request.get("keys", []))
    prefixes = tuple(
        f"{prefix['host']}{prefix.get('prefix', '/')}".encode()
        for prefix in request.get("prefixes", [])
    )
    tags = frozenset(tag.strip().lower().encode() for tag in request.get("tags", []))
    everything = bool(request.get("all"))
    if everything and (keys or prefixes or tags):
        print("emptying the cache cannot be combined with a selection", file=sys.stderr)
        return 2
    if not everything and not keys and not prefixes and not tags:
        print("no keys, prefixes or tags to purge", file=sys.stderr)
        return 2
    if not root.is_dir():
        # A missing cache directory is a misconfigured role, not an empty cache:
        # reporting success here would hide that nothing could ever be purged.
        print(f"cache directory {root} does not exist", file=sys.stderr)
        return 1
    tag_header = (
        str(request.get("tag_header") or DEFAULT_TAG_HEADER).strip().lower().encode()
    )
    if everything:
        report = empty(root, index=index, tag_header=tag_header)
    else:
        report = purge(
            root,
            index=index,
            keys=keys,
            prefixes=prefixes,
            tags=tags,
            tag_header=tag_header,
        )
    json.dump(report, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
