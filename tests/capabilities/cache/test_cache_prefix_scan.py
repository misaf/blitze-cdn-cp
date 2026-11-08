"""`purge_prefixes.py`, the script the purge role runs on an edge for prefixes.

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
    / "blitzecdn_cache_purge/files/purge_prefixes.py"
)


@pytest.fixture
def scanner():
    return runpy.run_path(str(SCRIPT))


def _store(cache: Path, key: str, *, body: bytes = b"<html>") -> Path:
    """Write one entry where proxy_cache_path levels=1:2 would."""
    digest = hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()
    path = cache / digest[-1] / digest[-3:-1] / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    # Version, timestamps and offsets: arbitrary bytes, newlines included.
    header = os.urandom(300).replace(b"KEY: ", b"xxxxx") + b"\n"
    path.write_bytes(
        header + b"\nKEY: " + key.encode() + b"\nHTTP/1.1 200 OK\r\n\r\n" + body
    )
    return path


def _run(scanner, cache: Path, prefixes, monkeypatch, capsys) -> dict:
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"path": str(cache), "prefixes": prefixes}))
    )
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

    assert report == {"scanned": 9, "removed": 4}
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
    assert "src: purge_prefixes.py" in tasks
    assert "blitzecdn_cache_purge_prefixes" in tasks
