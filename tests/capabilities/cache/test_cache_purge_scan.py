"""`purge_scan.py`, the script the purge role runs on an edge for prefixes and tags.

Cache files are laid out and headed as nginx writes them: the binary header,
then ``KEY: <key>``, then the upstream response. The key is the one
`cache-upstream.conf.j2` builds.
"""

import hashlib
import io
import json
import os
import runpy
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
) -> Path:
    """Write one entry where proxy_cache_path levels=1:2 would."""
    digest = hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()
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


def _run(scanner, cache: Path, prefixes, monkeypatch, capsys, *, tags=()) -> dict:
    request = {"path": str(cache), "prefixes": prefixes, "tags": list(tags)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    assert scanner["main"]() == 0
    return json.loads(capsys.readouterr().out)


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
    assert "blitzecdn_cache_purge_prefixes" in tasks
    assert "blitzecdn_cache_purge_tags" in tasks
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
