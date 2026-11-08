#!/usr/bin/env python3
"""Remove every cached response whose key names a hostname and path prefix.

Standalone standard-library code: Ansible copies this to an edge, where the
control-plane package is not installed. It reads a JSON request on stdin and
writes a JSON report on stdout.

nginx open source cannot delete by pattern, and a cache file's name is the MD5
of its key, so the key has to be read back from the file. nginx writes it as a
``KEY: <key>`` line in a header ahead of the body; only the head of each file
is read. The key is built by blitzecdn_nginx as

    $scheme $server_port $request_method $host <request target> <encoding>

with nothing between the parts. The scheme is lowercase letters, the port
digits and the method uppercase letters, and nginx lowercases ``$host``, so the
hostname starts at the first character after the method. Every request target
begins with ``/``, which is where the hostname ends: ``example.com/`` cannot
match a key for ``example.com.evil/``.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

#: Enough for nginx's binary header plus the longest key a purge can name.
HEAD_BYTES = 8192
KEY_MARKER = b"\nKEY: "
#: The scheme, listener port and method that lead every key.
KEY_LEAD = re.compile(rb"^(?:https?)[0-9]+[A-Z]+")
#: Files nginx renames into place are named by the full MD5. Anything else,
#: such as a temporary file still being written, is left alone.
CACHE_FILE = re.compile(r"^[0-9a-f]{32}$")


def cache_key(path: Path) -> bytes | None:
    """The key a cache file was stored under, or None if it cannot be read."""
    try:
        with path.open("rb") as handle:
            head = handle.read(HEAD_BYTES)
    except OSError:
        return None
    start = head.find(KEY_MARKER)
    if start < 0:
        return None
    start += len(KEY_MARKER)
    end = head.find(b"\n", start)
    if end < 0:
        return None
    return head[start:end]


def matches(key: bytes, targets: tuple[bytes, ...]) -> bool:
    lead = KEY_LEAD.match(key)
    if lead is None:
        return False
    return key[lead.end() :].startswith(targets)


def purge(root: Path, targets: tuple[bytes, ...]) -> dict[str, int]:
    """Walk one cache directory, on one filesystem, deleting what matches."""
    scanned = removed = 0
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
            scanned += 1
            key = cache_key(path)
            if key is None or not matches(key, targets):
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                # Evicted by nginx between the read and now: gone either way.
                continue
            removed += 1
    return {"scanned": scanned, "removed": removed}


def main() -> int:
    request: dict[str, Any] = json.load(sys.stdin)
    root = Path(request["path"])
    targets = tuple(
        f"{prefix['host']}{prefix.get('prefix', '/')}".encode()
        for prefix in request["prefixes"]
    )
    if not targets:
        print("no prefixes to purge", file=sys.stderr)
        return 2
    if not root.is_dir():
        # A missing cache directory is a misconfigured role, not an empty cache:
        # reporting success here would hide that nothing could ever be purged.
        print(f"cache directory {root} does not exist", file=sys.stderr)
        return 1
    json.dump(purge(root, targets), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
