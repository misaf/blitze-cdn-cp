#!/usr/bin/env python3
"""Remove every cached response with a key, under a prefix, or carrying a tag.

Standalone standard-library code: Ansible copies this to an edge, where the
control-plane package is not installed. It reads a JSON request on stdin and
writes a JSON report on stdout.

nginx open source cannot delete by pattern, and a cache file's name is the MD5
of its key, so what a file holds has to be read back from it. nginx writes a
cache file as a binary header, a ``KEY: <key>`` line, then the upstream
response exactly as the origin sent it: status line, header lines, a blank
line, the body. Keys, prefixes and tags are matched in one walk, reading only
the head of each file.

Keys
    A named purge gives the exact keys of a URL: one per scheme, listener
    port, method and encoding. Hashing a key gives the file of the entry it
    names, but not every file holding that key. When the origin answers with
    ``Vary``, nginx files the first representation under the MD5 of the key
    and every other one under a hash of the key plus the varied request
    headers, and it reopens such a secondary file whenever the request's
    headers select it. ``Vary: Accept-Encoding`` alone does this: the key
    names the normalized encoding, but nginx computes the variant from the
    visitor's raw header. Every one of those files carries the same KEY line,
    so matching it exactly is what removes them all.

Prefixes
    The key is built by blitzecdn_nginx as

        $scheme $server_port $request_method $host <request target> <encoding>

    with nothing between the parts. The scheme is lowercase letters, the port
    digits and the method uppercase letters, and nginx lowercases ``$host``,
    so the hostname starts at the first character after the method. Every
    request target begins with ``/``, which is where the hostname ends:
    ``example.com/`` cannot match a key for ``example.com.evil/``.

Tags
    The origin names an entry's tags in a response header, ``Cache-Tag:
    product-42, listing`` by default. nginx stores that header with the entry
    even though the edge hides it from visitors, so the cache is its own tag
    index: it is bounded by ``proxy_cache_path max_size``, and a tag leaves the
    index exactly when nginx evicts or replaces the entry carrying it. Nothing
    can drift out of step with it or be lost on a restart.

    Header names match case-insensitively; the header may appear more than
    once; values are split on commas and trimmed. Tags are case-insensitive:
    ``Product-42`` and ``product-42`` are one tag. A purge would rather remove
    an entry it was not sure about than leave one serving stale content.

Files that cannot be decided
    A tag purge needs the whole response header. nginx refuses an upstream
    header larger than ``proxy_buffer_size`` (a page or two by default), so
    the header ends well inside ``MAX_HEAD_BYTES``. If it does not, or a file
    has no KEY line, the file is counted as ``unchecked`` rather than as a
    non-match: the role fails the edge, so a purge cannot report success
    while an entry it should have removed is still being served.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Iterable
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


def under_prefix(key: bytes, prefixes: tuple[bytes, ...]) -> bool:
    if not prefixes:
        return False
    lead = KEY_LEAD.match(key)
    if lead is None:
        return False
    return key[lead.end() :].startswith(prefixes)


def cache_files(root: Path) -> Iterable[Path]:
    """Every file nginx has renamed into place, on the cache's own filesystem."""
    device = root.stat().st_dev
    for directory, subdirectories, files in os.walk(root):
        # Like find -xdev: a filesystem mounted under the cache is not the cache.
        subdirectories[:] = [
            name
            for name in subdirectories
            if (Path(directory) / name).lstat().st_dev == device
        ]
        for name in files:
            if not CACHE_FILE.match(name):
                continue
            path = Path(directory) / name
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
            except OSError:
                continue
            yield path


def purge(
    root: Path,
    *,
    keys: frozenset[bytes] = frozenset(),
    prefixes: tuple[bytes, ...] = (),
    tags: frozenset[bytes] = frozenset(),
    tag_header: bytes = DEFAULT_TAG_HEADER.lower().encode(),
) -> dict[str, int]:
    """Walk one cache directory once, deleting what any key, prefix or tag matches."""
    scanned = removed = unchecked = 0
    for path in cache_files(root):
        try:
            head = read_head(path, need_header=bool(tags))
        except FileNotFoundError:
            # Evicted by nginx between the walk and the read: gone either way.
            continue
        except OSError:
            scanned += 1
            unchecked += 1
            continue
        scanned += 1
        if head.key is None:
            unchecked += 1
            continue
        if under_prefix(head.key, prefixes) or (head.whole_key and head.key in keys):
            matched = True
        elif not head.whole_key and any(key.startswith(head.key) for key in keys):
            # Cut off where a requested key could still continue: undecided.
            unchecked += 1
            continue
        elif not tags:
            matched = False
        elif head.header is None:
            unchecked += 1
            continue
        else:
            matched = not tags.isdisjoint(header_tags(head.header, tag_header))
        if not matched:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed += 1
    return {"scanned": scanned, "removed": removed, "unchecked": unchecked}


def main() -> int:
    request: dict[str, Any] = json.load(sys.stdin)
    root = Path(request["path"])
    keys = frozenset(key.encode() for key in request.get("keys", []))
    prefixes = tuple(
        f"{prefix['host']}{prefix.get('prefix', '/')}".encode()
        for prefix in request.get("prefixes", [])
    )
    tags = frozenset(tag.strip().lower().encode() for tag in request.get("tags", []))
    if not keys and not prefixes and not tags:
        print("no keys, prefixes or tags to purge", file=sys.stderr)
        return 2
    if not root.is_dir():
        # A missing cache directory is a misconfigured role, not an empty cache:
        # reporting success here would hide that nothing could ever be purged.
        print(f"cache directory {root} does not exist", file=sys.stderr)
        return 1
    tag_header = str(request.get("tag_header") or DEFAULT_TAG_HEADER)
    json.dump(
        purge(
            root,
            keys=keys,
            prefixes=prefixes,
            tags=tags,
            tag_header=tag_header.strip().lower().encode(),
        ),
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
