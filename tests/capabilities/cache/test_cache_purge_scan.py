"""`purge_scan.py`, the script the purge role runs on an edge.

It matches keys, prefixes and tags in one walk.

Cache files are laid out and headed as nginx writes them: the binary header,
then ``KEY: <key>``, then the upstream response. The key is the one
`cache-upstream.conf.j2` builds.
"""

import hashlib
import io
import json
import os
import runpy
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest
from support.paths import SOURCE

SCRIPT = (
    SOURCE
    / "capabilities/cache/ansible/roles"
    / "blitzecdn_cache_purge/files/purge_scan.py"
)


@pytest.fixture
def scanner():
    return runpy.run_path(str(SCRIPT))


def _store(
    cache: Path,
    key: str,
    *,
    body: bytes = b"<html>",
    headers: bytes = b"Content-Type: text/html\r\n",
    variant: str = "",
) -> Path:
    """Write one entry where proxy_cache_path levels=1:2 would.

    A ``variant`` stands for the values of the request headers an origin's
    ``Vary`` names: nginx files that representation under a hash of the key and
    those values, while its KEY line stays the key.
    """
    digest = hashlib.md5((key + variant).encode(), usedforsecurity=False).hexdigest()
    path = cache / digest[-1] / digest[-3:-1] / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    # Version, timestamps and offsets: arbitrary bytes, newlines included.
    header = os.urandom(300).replace(b"KEY: ", b"xxxxx") + b"\n"
    path.write_bytes(
        header
        + b"\nKEY: "
        + key.encode()
        + b"\nHTTP/1.1 200 OK\r\n"
        + headers
        + b"\r\n"
        + body
    )
    return path


def _run(
    scanner,
    cache: Path,
    prefixes,
    monkeypatch,
    capsys,
    *,
    tags=(),
    keys=(),
    everything=False,
    keep_read=False,
) -> dict:
    """Run the scanner as the role does, with an index beside the cache.

    How many files were read depends on the index, not on what matched, so it
    is dropped unless a test is about the index.
    """
    request = {
        "path": str(cache),
        "index": str(cache.parent / "index" / "cache-index.sqlite3"),
        "all": everything,
        "keys": list(keys),
        "prefixes": prefixes,
        "tags": list(tags),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    assert scanner["main"]() == 0
    report = json.loads(capsys.readouterr().out)
    if not keep_read:
        report.pop("read")
    return report


def _settle(cache: Path) -> None:
    """Age every directory past the window in which the index distrusts it."""
    past = time.time_ns() - 3600 * 10**9
    for directory, _subdirectories, _files in os.walk(cache):
        os.utime(directory, ns=(past, past))


def test_a_prefix_removes_every_variant_under_it_and_nothing_else(
    tmp_path, scanner, monkeypatch, capsys
):
    cache = tmp_path / "cache"
    under = [
        _store(cache, "https443GETcdn.example.com/blog/post"),
        _store(cache, "https8443HEADcdn.example.com/blog/post"),
        _store(cache, "http80GETcdn.example.com/blog/?page=2gzip"),
        _store(cache, "https443GETcdn.example.com/blog/a.cssbr"),
    ]
    kept = [
        _store(cache, "https443GETcdn.example.com/blogroll"),
        _store(cache, "https443GETcdn.example.com/"),
        _store(cache, "https443GETother.example.com/blog/post"),
        # A longer hostname with ours as its start is a different host.
        _store(cache, "https443GETcdn.example.com.evil/blog/post"),
        # The KEY line comes from nginx's header, never from the body.
        _store(
            cache, "https443GETx.example.com/", body=b"\nKEY: cdn.example.com/blog/"
        ),
    ]

    report = _run(
        scanner,
        cache,
        [{"host": "cdn.example.com", "prefix": "/blog/"}],
        monkeypatch,
        capsys,
    )

    assert report == {"scanned": 9, "removed": 4, "unchecked": 0}
    assert not any(path.exists() for path in under)
    assert all(path.exists() for path in kept)


def test_a_hostname_purge_leaves_other_hostnames_and_the_levels(
    tmp_path, scanner, monkeypatch, capsys
):
    cache = tmp_path / "cache"
    gone = _store(cache, "https443GETcdn.example.com/a")
    kept = _store(cache, "https443GETapi.example.com/a")
    # A temporary file nginx is still writing: not named by an MD5, not touched.
    temporary = gone.parent / f"{gone.name}.0000000001"
    temporary.write_bytes(gone.read_bytes())

    report = _run(scanner, cache, [{"host": "cdn.example.com"}], monkeypatch, capsys)

    assert report["removed"] == 1
    assert not gone.exists()
    assert kept.exists()
    assert temporary.exists()
    # nginx created the level directories and expects them to stay.
    assert gone.parent.is_dir()


def test_a_key_longer_than_the_read_still_matches_by_its_start(
    tmp_path, scanner, monkeypatch, capsys
):
    """nginx caches request targets up to its 8k header buffer, past HEAD_BYTES."""
    cache = tmp_path / "cache"
    long_key = "https443GETcdn.example.com/blog/" + "a" * 8100
    gone = _store(cache, long_key)

    report = _run(
        scanner,
        cache,
        [{"host": "cdn.example.com", "prefix": "/blog/"}],
        monkeypatch,
        capsys,
    )

    assert report == {"scanned": 1, "removed": 1, "unchecked": 0}
    assert not gone.exists()


# ----------------------------------------------------------------------
# Keys
# ----------------------------------------------------------------------


def test_a_key_removes_every_vary_variant_and_nothing_else(
    tmp_path, scanner, monkeypatch, capsys
):
    """Issue 253: a variant nginx filed under its own hash is still the key's."""
    cache = tmp_path / "cache"
    key = "https443GETcdn.example.com/app.jsbr"
    gone = [
        _store(cache, key),
        # Vary: Accept-Encoding, browsers with and without zstd.
        _store(cache, key, variant="gzip, deflate, br"),
        _store(cache, key, variant="gzip, deflate, br, zstd"),
    ]
    kept = [
        # The same URL under another encoding or method is another key.
        _store(cache, "https443GETcdn.example.com/app.jsgzip"),
        _store(cache, "https443HEADcdn.example.com/app.jsbr"),
        # A key that merely starts with ours is another URL.
        _store(cache, "https443GETcdn.example.com/app.jsbr2"),
        _store(cache, "https443GETcdn.example.com/app.js.mapbr"),
    ]

    report = _run(scanner, cache, [], monkeypatch, capsys, keys=[key])

    assert report == {"scanned": 7, "removed": 3, "unchecked": 0}
    assert not any(path.exists() for path in gone)
    assert all(path.exists() for path in kept)


def test_a_key_cut_off_by_the_read_is_unchecked_if_it_could_be_requested(
    tmp_path, scanner, monkeypatch, capsys
):
    long_key = "https443GETcdn.example.com/" + "a" * scanner["MAX_HEAD_BYTES"]
    path = _store(cache := tmp_path / "cache", long_key)
    other = _store(cache, "https443GETother.example.com/" + "a" * 70000)

    report = _run(scanner, cache, [], monkeypatch, capsys, keys=[long_key])

    assert report == {"scanned": 2, "removed": 0, "unchecked": 1}
    assert path.exists()
    assert other.exists()


def test_a_missing_cache_directory_is_a_failure_not_an_empty_purge(
    tmp_path, scanner, monkeypatch
):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {"path": str(tmp_path / "absent"), "prefixes": [{"host": "a.com"}]}
            )
        ),
    )
    assert scanner["main"]() != 0


def test_the_purge_role_runs_the_scanner_it_ships():
    tasks = (SCRIPT.parent.parent / "tasks/main.yml").read_text(encoding="utf-8")
    assert "src: purge_scan.py" in tasks
    assert "blitzecdn_cache_purge_keys" in tasks
    assert "blitzecdn_cache_purge_prefixes" in tasks
    assert "blitzecdn_cache_purge_tags" in tasks
    assert "blitzecdn_cache_purge_index_path" in tasks
    # Emptying the cache goes through the scanner too, so it reports counts.
    assert "blitzecdn_cache_purge_all | bool" in tasks
    assert "find " not in tasks
    # An undecided file fails the edge rather than passing as a non-match.
    assert "blitzecdn_report.unchecked | int == 0" in tasks


def test_the_edge_hides_cache_tags_from_visitors_but_keeps_them_cached():
    upstream = (SOURCE / "capabilities/cache/nginx/cache-upstream.conf.j2").read_text(
        encoding="utf-8"
    )
    assert "proxy_hide_header Cache-Tag;" in upstream


# ----------------------------------------------------------------------
# Tags
# ----------------------------------------------------------------------


def test_a_tag_removes_every_entry_whose_stored_header_names_it(
    tmp_path, scanner, monkeypatch, capsys
):
    cache = tmp_path / "cache"
    gone = [
        _store(
            cache, "https443GETa.example.com/p/42", headers=b"Cache-Tag: product-42\r\n"
        ),
        # Any hostname, other tags alongside, odd spacing.
        _store(
            cache,
            "http80GETb.example.org/list",
            headers=b"Cache-Tag:listing ,  product-42,home\r\n",
        ),
        # Header names are case-insensitive; so are tags.
        _store(
            cache, "https443GETa.example.com/x", headers=b"cache-tag: PRODUCT-42\r\n"
        ),
        # The header may repeat.
        _store(
            cache,
            "https443GETa.example.com/y",
            headers=b"Cache-Tag: home\r\nX-Other: 1\r\nCache-Tag: product-42\r\n",
        ),
        # Bare LF line endings, as nginx also accepts from an upstream.
        _store(cache, "https443GETa.example.com/z", headers=b"Cache-Tag: product-42\n"),
    ]
    kept = [
        # A tag that merely starts with ours is another tag.
        _store(
            cache, "https443GETa.example.com/43", headers=b"Cache-Tag: product-420\r\n"
        ),
        # Another header whose value names the tag is not a tag.
        _store(
            cache, "https443GETa.example.com/n", headers=b"X-Cache-Tag: product-42\r\n"
        ),
        _store(cache, "https443GETa.example.com/none"),
        # The body is not the header.
        _store(
            cache,
            "https443GETa.example.com/body",
            body=b"Cache-Tag: product-42\r\n\r\n",
        ),
    ]

    report = _run(scanner, cache, [], monkeypatch, capsys, tags=["Product-42"])

    assert report == {"scanned": 9, "removed": 5, "unchecked": 0}
    assert not any(path.exists() for path in gone)
    assert all(path.exists() for path in kept)


def test_prefixes_and_tags_are_matched_in_one_walk(
    tmp_path, scanner, monkeypatch, capsys
):
    cache = tmp_path / "cache"
    by_prefix = _store(cache, "https443GETa.example.com/blog/1")
    by_tag = _store(cache, "https443GETb.example.com/", headers=b"Cache-Tag: home\r\n")
    kept = _store(cache, "https443GETb.example.com/other")

    report = _run(
        scanner,
        cache,
        [{"host": "a.example.com", "prefix": "/blog/"}],
        monkeypatch,
        capsys,
        tags=["home"],
    )

    assert report == {"scanned": 3, "removed": 2, "unchecked": 0}
    assert not by_prefix.exists()
    assert not by_tag.exists()
    assert kept.exists()


def test_a_header_past_the_first_read_is_read_to_its_end(
    tmp_path, scanner, monkeypatch, capsys
):
    """A large Set-Cookie or CSP header pushes Cache-Tag past HEAD_BYTES."""
    cache = tmp_path / "cache"
    gone = _store(
        cache,
        "https443GETa.example.com/",
        headers=b"Content-Security-Policy: " + b"a" * 20000 + b"\r\nCache-Tag: big\r\n",
    )

    report = _run(scanner, cache, [], monkeypatch, capsys, tags=["big"])

    assert report == {"scanned": 1, "removed": 1, "unchecked": 0}
    assert not gone.exists()


def test_a_header_past_the_hard_cap_is_unchecked_not_a_non_match(
    tmp_path, scanner, monkeypatch, capsys
):
    """The role fails the edge on it, so the purge cannot claim success."""
    cache = tmp_path / "cache"
    huge = _store(
        cache,
        "https443GETa.example.com/",
        headers=b"X-Huge: "
        + b"a" * scanner["MAX_HEAD_BYTES"]
        + b"\r\nCache-Tag: big\r\n",
    )
    # A truncated file whose header never ends is undecided too.
    truncated = _store(cache, "https443GETa.example.com/t")
    truncated.write_bytes(
        truncated.read_bytes().split(b"HTTP/1.1")[0] + b"HTTP/1.1 200"
    )

    report = _run(scanner, cache, [], monkeypatch, capsys, tags=["big"])

    assert report == {"scanned": 2, "removed": 0, "unchecked": 2}
    assert huge.exists()
    assert truncated.exists()


def test_an_undecided_file_still_goes_when_a_prefix_matches_its_key(
    tmp_path, scanner, monkeypatch, capsys
):
    cache = tmp_path / "cache"
    huge = _store(
        cache,
        "https443GETa.example.com/blog/",
        headers=b"X-Huge: " + b"a" * scanner["MAX_HEAD_BYTES"] + b"\r\n",
    )

    report = _run(
        scanner,
        cache,
        [{"host": "a.example.com", "prefix": "/blog/"}],
        monkeypatch,
        capsys,
        tags=["big"],
    )

    assert report == {"scanned": 1, "removed": 1, "unchecked": 0}
    assert not huge.exists()


def test_a_prefix_only_scan_reads_no_further_than_the_key(scanner, tmp_path):
    cache = tmp_path / "cache"
    path = _store(
        cache,
        "https443GETa.example.com/",
        headers=b"X-Huge: " + b"a" * scanner["MAX_HEAD_BYTES"] + b"\r\n",
    )
    head = scanner["read_head"](path, need_header=False)
    assert head.key == b"https443GETa.example.com/"
    assert head.header is None


def test_a_file_without_a_key_line_is_unchecked(tmp_path, scanner, monkeypatch, capsys):
    cache = tmp_path / "cache"
    path = _store(cache, "https443GETa.example.com/")
    path.write_bytes(b"garbage with no key line")

    report = _run(scanner, cache, [{"host": "a.example.com"}], monkeypatch, capsys)

    assert report == {"scanned": 1, "removed": 0, "unchecked": 1}
    assert path.exists()


def test_a_folded_line_of_another_header_is_not_part_of_a_tag(scanner):
    tags = scanner["header_tags"]
    assert tags(
        b"Cache-Tag: product-42\r\nX-Other: first\r\n continuation\r\n", b"cache-tag"
    ) == {b"product-42"}
    # Folded before any tag header: ignored, not an IndexError.
    assert tags(
        b"X-Other: first\r\n continuation\r\nCache-Tag: home\r\n", b"cache-tag"
    ) == {b"home"}
    # A folded Cache-Tag continues its own value.
    assert tags(b"Cache-Tag: a,\r\n b\r\n", b"cache-tag") == {b"a", b"b"}


# ----------------------------------------------------------------------
# The index
# ----------------------------------------------------------------------


def test_an_unchanged_cache_is_purged_without_reading_a_file(
    tmp_path, scanner, monkeypatch, capsys
):
    cache = tmp_path / "cache"
    tagged = _store(
        cache, "https443GETa.example.com/p", headers=b"Cache-Tag: product\r\n"
    )
    for number in range(20):
        _store(cache, f"https443GETa.example.com/{number}")
    _settle(cache)

    first = _run(scanner, cache, [], monkeypatch, capsys, tags=["none"], keep_read=True)
    second = _run(
        scanner, cache, [], monkeypatch, capsys, tags=["product"], keep_read=True
    )

    assert first == {"scanned": 21, "read": 21, "removed": 0, "unchecked": 0}
    # Resolved through the index alone: the tag was found without a read.
    assert second == {"scanned": 21, "read": 0, "removed": 1, "unchecked": 0}
    assert not tagged.exists()


def test_only_entries_stored_since_the_last_purge_are_read(
    tmp_path, scanner, monkeypatch, capsys
):
    cache = tmp_path / "cache"
    for number in range(20):
        _store(cache, f"https443GETa.example.com/{number}")
    _settle(cache)
    _run(scanner, cache, [], monkeypatch, capsys, tags=["none"])

    new = _store(cache, "https443GETb.example.com/new", headers=b"Cache-Tag: new\r\n")
    report = _run(scanner, cache, [], monkeypatch, capsys, tags=["new"], keep_read=True)

    assert report == {"scanned": 21, "read": 1, "removed": 1, "unchecked": 0}
    assert not new.exists()


def test_an_entry_replaced_under_its_name_is_read_again(
    tmp_path, scanner, monkeypatch, capsys
):
    """nginx replaces an entry by renaming a new file over it: new tags too."""
    cache = tmp_path / "cache"
    key = "https443GETa.example.com/p"
    path = _store(cache, key, headers=b"Cache-Tag: old\r\n")
    _settle(cache)
    _run(scanner, cache, [], monkeypatch, capsys, tags=["none"])

    replacement = path.with_name(path.name + ".0000000001")
    replacement.write_bytes(path.read_bytes().replace(b"old", b"new"))
    replacement.rename(path)
    report = _run(scanner, cache, [], monkeypatch, capsys, tags=["new"], keep_read=True)

    assert report == {"scanned": 1, "read": 1, "removed": 1, "unchecked": 0}
    assert not path.exists()


def test_an_evicted_entry_leaves_the_index(tmp_path, scanner, monkeypatch, capsys):
    cache = tmp_path / "cache"
    evicted = _store(cache, "https443GETa.example.com/old")
    _store(cache, "https443GETa.example.com/kept")
    _settle(cache)
    _run(scanner, cache, [], monkeypatch, capsys, tags=["none"])

    evicted.unlink()
    # nginx's cache manager can remove a whole level directory's last entry.
    report = _run(
        scanner, cache, [{"host": "a.example.com"}], monkeypatch, capsys, keep_read=True
    )

    assert report == {"scanned": 1, "read": 0, "removed": 1, "unchecked": 0}


def test_a_directory_changed_too_recently_to_trust_is_listed_again(
    tmp_path, scanner, monkeypatch, capsys
):
    """A store in the same clock tick as a listing leaves the mtime unchanged."""
    cache = tmp_path / "cache"
    first = _store(cache, "https443GETa.example.com/1")
    _run(scanner, cache, [], monkeypatch, capsys, tags=["none"])

    # Same directory, and its mtime is put back as if nothing had happened.
    status = first.parent.stat()
    second = first.parent / ("f" * 32)
    second.write_bytes(first.read_bytes().replace(b"/1\n", b"/2\n"))
    os.utime(first.parent, ns=(status.st_atime_ns, status.st_mtime_ns))
    report = _run(scanner, cache, [{"host": "a.example.com"}], monkeypatch, capsys)

    assert report == {"scanned": 2, "removed": 2, "unchecked": 0}
    assert not second.exists()


def test_a_corrupt_index_is_rebuilt_from_the_cache(
    tmp_path, scanner, monkeypatch, capsys
):
    cache = tmp_path / "cache"
    gone = _store(cache, "https443GETa.example.com/")
    index = tmp_path / "index" / "cache-index.sqlite3"
    index.parent.mkdir()
    index.write_bytes(b"not a database" * 100)

    report = _run(scanner, cache, [{"host": "a.example.com"}], monkeypatch, capsys)

    assert report == {"scanned": 1, "removed": 1, "unchecked": 0}
    assert not gone.exists()
    with closing(sqlite3.connect(index)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_an_index_of_another_cache_is_not_trusted(
    tmp_path, scanner, monkeypatch, capsys
):
    first = tmp_path / "cache"
    _store(first, "https443GETa.example.com/")
    _settle(first)
    _run(scanner, first, [], monkeypatch, capsys, tags=["none"])

    # The same index, pointed at another directory with the same layout.
    second = tmp_path / "moved"
    first.rename(second)
    gone = _store(second, "https443GETb.example.com/")
    _settle(second)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "path": str(second),
                    "index": str(tmp_path / "index" / "cache-index.sqlite3"),
                    "prefixes": [{"host": "b.example.com"}],
                }
            )
        ),
    )
    assert scanner["main"]() == 0
    report = json.loads(capsys.readouterr().out)

    assert report == {"scanned": 2, "read": 2, "removed": 1, "unchecked": 0}
    assert not gone.exists()


def test_the_index_is_private(tmp_path, scanner, monkeypatch, capsys):
    cache = tmp_path / "cache"
    _store(cache, "https443GETa.example.com/")
    _run(scanner, cache, [{"host": "b.example.com"}], monkeypatch, capsys)

    index = tmp_path / "index" / "cache-index.sqlite3"
    assert index.stat().st_mode & 0o077 == 0


# ----------------------------------------------------------------------
# Emptying the cache
# ----------------------------------------------------------------------


def test_emptying_the_cache_reports_what_it_removed_and_keeps_the_levels(
    tmp_path, scanner, monkeypatch, capsys
):
    cache = tmp_path / "cache"
    stored = [_store(cache, f"https443GETa.example.com/{n}") for n in range(5)]
    # A temporary file goes too, as find -type f -delete took it.
    temporary = stored[0].parent / f"{stored[0].name}.0000000001"
    temporary.write_bytes(b"partial")
    _run(scanner, cache, [], monkeypatch, capsys, tags=["none"])

    report = _run(scanner, cache, [], monkeypatch, capsys, everything=True)

    assert report == {"scanned": 6, "removed": 6, "unchecked": 0}
    assert not any(path.exists() for path in [*stored, temporary])
    assert stored[0].parent.is_dir()
    # The index went with the cache: a new entry is found by the next purge.
    gone = _store(cache, "https443GETa.example.com/0")
    report = _run(scanner, cache, [{"host": "a.example.com"}], monkeypatch, capsys)
    assert report == {"scanned": 1, "removed": 1, "unchecked": 0}
    assert not gone.exists()


def test_emptying_the_cache_cannot_be_combined_with_a_selection(
    tmp_path, scanner, monkeypatch
):
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {"path": str(tmp_path / "cache"), "all": True, "tags": ["product"]}
            )
        ),
    )
    assert scanner["main"]() == 2
