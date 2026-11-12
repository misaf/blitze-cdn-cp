"""The purge scanner against a real nginx cache that keeps changing under it.

`test_cache_purge_scan.py` lays cache files out by hand. Here nginx writes
them: a real nginx, started as this user on a loopback port, proxies a small
origin in this process with the shipped cache key, and the shipped scanner
purges its cache while it serves. What a purge promises about entries nginx
stores, replaces or evicts while it runs is in docs/design/cache.md
("Concurrent cache activity"); these tests hold the scanner to it:

* every matching file in place when the scan began is gone afterwards, every
  ``Vary`` representation included, unless the report says ``unchecked``;
* nginx then fetches those URLs from the origin again;
* an entry that lands after the scan began may survive, and the next purge
  removes it;
* a cold, a warm and a rebuilt index give the same result.

Where the order of events matters, the test makes it happen at a fixed point
of the purge rather than racing it: the origin holds a response until told to
answer, and the scanner's index refresh is wrapped to run the test's action
between the refresh and the deletions. Only the load test races, and it
asserts what holds whatever the interleaving.

nginx is needed on PATH. Without it these tests skip; CI installs it and sets
``BLITZE_REQUIRE_NGINX`` so a missing nginx fails instead. A failed test copies
nginx's configuration and logs, the index and a listing of the cache to
``DIAGNOSTICS_DIR`` when that is set.
"""

from __future__ import annotations

import http.client
import importlib.util
import io
import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import pytest
import yaml
from support.paths import SOURCE

pytestmark = pytest.mark.nginx

CACHE = SOURCE / "capabilities/cache"
SCRIPT = CACHE / "ansible/roles/blitzecdn_cache_purge/files/purge_scan.py"
ROLE_DEFAULTS = CACHE / "ansible/roles/blitzecdn_cache_purge/defaults/main.yml"
UPSTREAM_TEMPLATE = CACHE / "nginx/cache-upstream.conf.j2"
HTTP_TEMPLATE = SOURCE / "assets/ansible/roles/blitzecdn_nginx/templates/http.conf.j2"
TAG_HEADER = b"cache-tag"
CACHE_FILE = re.compile(r"^[0-9a-f]{32}$")
#: How long a test waits for nginx or the origin before calling it a failure.
PATIENCE = 10.0


def _nginx() -> str:
    found = shutil.which("nginx") or next(
        (
            candidate
            for candidate in ("/usr/sbin/nginx", "/usr/local/sbin/nginx")
            if os.access(candidate, os.X_OK)
        ),
        None,
    )
    if found is None:
        reason = "nginx is not installed"
        if os.environ.get("BLITZE_REQUIRE_NGINX"):
            pytest.fail(reason)
        pytest.skip(reason)
    return found


# ----------------------------------------------------------------------
# What the edge ships, read from the files that ship it
# ----------------------------------------------------------------------


def _shipped_cache_key() -> str:
    """``proxy_cache_key`` as cache-upstream.conf.j2 renders it for a site."""
    template = UPSTREAM_TEMPLATE.read_text(encoding="utf-8")
    found = re.search(r'proxy_cache_key "([^"]+)";', template)
    assert found, "cache-upstream.conf.j2 no longer sets proxy_cache_key"
    # The default query-string mode keys on the whole request target.
    return found.group(1).replace("{{ cache_uri }}", "$request_uri")


def _shipped_encoding_map() -> str:
    """The map that normalizes Accept-Encoding into the key, from http.conf.j2."""
    template = HTTP_TEMPLATE.read_text(encoding="utf-8")
    found = re.search(
        r"^map \$http_accept_encoding \$blitzecdn_accept_encoding \{.*?^\}",
        template,
        re.MULTILINE | re.DOTALL,
    )
    assert found, "http.conf.j2 no longer maps $blitzecdn_accept_encoding"
    return found.group(0)


def _role_defaults() -> dict[str, Any]:
    return yaml.safe_load(ROLE_DEFAULTS.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# The origin
# ----------------------------------------------------------------------


@dataclass
class Hold:
    """Keep the origin's next answer for one URL until the test releases it."""

    arrived: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)


class Origin:
    """An origin whose every answer says how many times it has answered.

    Each response carries ``Vary: X-Variant``, so nginx files the first
    representation of a URL at the MD5 of its key and every other one under a
    hash of the key and the variant: requesting one URL with two variants makes
    a primary and a secondary cache file. Tags are given per path by
    ``tags_for``.
    """

    def __init__(self, *, body_size: int = 256) -> None:
        self.body_size = body_size
        self.tags_for: Callable[[str], str] = lambda _path: ""
        self.fetches: Counter[tuple[str, str, str]] = Counter()
        self._holds: dict[tuple[str, str], Hold] = {}
        self._lock = threading.Lock()
        self._version = 0
        origin = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                origin.answer(self)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def hold(self, host: str, path: str) -> Hold:
        hold = Hold()
        with self._lock:
            self._holds[(host, path)] = hold
        return hold

    def answer(self, request: BaseHTTPRequestHandler) -> None:
        host = request.headers.get("Host", "")
        variant = request.headers.get("X-Variant", "")
        with self._lock:
            hold = self._holds.pop((host, request.path), None)
        if hold is not None:
            hold.arrived.set()
            hold.release.wait(PATIENCE)
        with self._lock:
            self._version += 1
            version = self._version
            self.fetches[(host, request.path, variant)] += 1
        head = f"{host}{request.path} variant={variant} v={version}\n".encode()
        body = head + b"." * max(0, self.body_size - len(head))
        request.send_response(200)
        request.send_header("Content-Type", "text/plain")
        request.send_header("Cache-Control", "max-age=3600")
        request.send_header("Vary", "X-Variant")
        request.send_header("X-Origin-Version", str(version))
        tags = self.tags_for(request.path)
        if tags:
            request.send_header("Cache-Tag", tags)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    def close(self) -> None:
        for hold in list(self._holds.values()):
            hold.release.set()
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(PATIENCE)


# ----------------------------------------------------------------------
# The edge: nginx in front of the origin, with the shipped cache key
# ----------------------------------------------------------------------


class Seen(NamedTuple):
    """What a visitor got: nginx's cache status and the origin's answer number."""

    cache: str
    version: int


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Edge:
    """A disposable nginx caching the origin, laid out as the edge lays it out."""

    def __init__(
        self, workdir: Path, origin: Origin, *, max_size: str, inactive: str
    ) -> None:
        self.workdir = workdir
        self.origin = origin
        self.cache = workdir / "cache"
        self.index = workdir / "lib" / "cache-index.sqlite3"
        self.port = _free_port()
        self.cache.mkdir()
        for temp in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"):
            (workdir / temp).mkdir()
        self.conf = workdir / "nginx.conf"
        self.error_log = workdir / "error.log"
        self.access_log = workdir / "access.log"
        self.conf.write_text(self._config(max_size, inactive), encoding="utf-8")
        self.process = subprocess.Popen(  # noqa: S603 - nginx over files this test wrote
            [
                _nginx(),
                "-p",
                str(workdir),
                "-c",
                str(self.conf),
                "-e",
                str(self.error_log),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_until_listening()

    def _config(self, max_size: str, inactive: str) -> str:
        work = self.workdir
        return f"""
daemon off;
master_process on;
worker_processes 1;
pid {work}/nginx.pid;
error_log {self.error_log} info;
events {{ worker_connections 512; }}
http {{
    log_format purge '$msec $request_method $host$request_uri '
                     'variant=$http_x_variant refill=$http_x_refill '
                     '$upstream_cache_status $status '
                     'rt=$request_time urt=$upstream_response_time';
    access_log {self.access_log} purge;
    client_body_temp_path {work}/client_body;
    proxy_temp_path {work}/proxy;
    fastcgi_temp_path {work}/fastcgi;
    uwsgi_temp_path {work}/uwsgi;
    scgi_temp_path {work}/scgi;

    {_shipped_encoding_map()}

    # As cache-http.conf.j2 declares it, with a manager that wakes often
    # enough for a small max_size to evict while a test runs.
    proxy_cache_path {self.cache}
        levels=1:2
        keys_zone=purge_test:4m
        max_size={max_size}
        inactive={inactive}
        use_temp_path=off
        manager_sleep=50ms
        manager_threshold=50ms;

    server {{
        listen 127.0.0.1:{self.port};
        location / {{
            proxy_pass http://127.0.0.1:{self.origin.port};
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header Connection "";
            proxy_cache purge_test;
            proxy_cache_key "{_shipped_cache_key()}";
            proxy_cache_valid 200 10m;
            proxy_cache_lock on;
            proxy_hide_header Cache-Tag;
            proxy_set_header Accept-Encoding $blitzecdn_accept_encoding;
            # Not in the shipped configuration: a way for the test to make
            # nginx fetch and store an entry that is already cached, which
            # replaces its file the way an expired entry's refetch does.
            proxy_cache_bypass $http_x_refill;
            add_header X-Cache-Status $upstream_cache_status always;
        }}
    }}
}}
"""

    def _wait_until_listening(self) -> None:
        deadline = time.monotonic() + PATIENCE
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(
                    f"nginx exited with {self.process.returncode}:\n{self.log_tail()}"
                )
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
            except OSError:
                time.sleep(0.02)
                continue
            return
        raise AssertionError(f"nginx did not listen on {self.port}:\n{self.log_tail()}")

    def get(
        self,
        host: str,
        path: str,
        *,
        variant: str = "",
        encoding: str = "",
        refill: bool = False,
        connection: http.client.HTTPConnection | None = None,
    ) -> Seen:
        """Request one URL, on ``connection`` if given (kept open), else a new one."""
        headers = {"Host": host}
        if variant:
            headers["X-Variant"] = variant
        if encoding:
            headers["Accept-Encoding"] = encoding
        if refill:
            headers["X-Refill"] = "1"
        own = connection is None
        if connection is None:
            connection = self.connect()
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            body = response.read()
            status = response.getheader("X-Cache-Status", "")
        finally:
            if own:
                connection.close()
        assert response.status == 200, (response.status, body[:200])
        version = re.search(rb" v=(\d+)\n", body)
        assert version, body[:200]
        return Seen(status, int(version.group(1)))

    def connect(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=PATIENCE)

    def files(self) -> dict[Path, os.stat_result]:
        """Every file nginx has renamed into the cache, as it is now."""
        found: dict[Path, os.stat_result] = {}
        for directory, _subdirectories, names in os.walk(self.cache):
            for name in names:
                if CACHE_FILE.match(name):
                    path = Path(directory) / name
                    try:
                        found[path] = path.stat()
                    except FileNotFoundError:
                        continue
        return found

    def wait_for_file(self, predicate: Callable[[Entry], bool]) -> Entry:
        """Wait until nginx has renamed a file into the cache that matches."""
        deadline = time.monotonic() + PATIENCE
        while time.monotonic() < deadline:
            for entry in entries(self):
                if predicate(entry):
                    return entry
            time.sleep(0.01)
        raise AssertionError("nginx never stored the expected cache file")

    def start_evicting(self) -> None:
        """Make nginx's cache manager evict from now on, not ten seconds later.

        The manager sleeps ten seconds after a pass over an empty cache, and
        its first pass is at startup. A reload starts a new manager, which
        finds one entry and from then on wakes when the oldest entry goes
        inactive, or ``manager_sleep`` after evicting for ``max_size``.

        Returns once the old worker has exited: until then it may accept a
        connection and close it as it shuts down.
        """
        self.get("warm.test", "/")
        self.process.send_signal(signal.SIGHUP)
        deadline = time.monotonic() + PATIENCE
        while time.monotonic() < deadline:
            log = self.error_log.read_text(encoding="utf-8", errors="replace")
            if log.count("start cache manager process") >= 2 and re.search(
                r"worker process \d+ exited", log
            ):
                return
            time.sleep(0.02)
        raise AssertionError(f"nginx did not reload:\n{self.log_tail()}")

    def serious_errors(self) -> list[str]:
        """Error log lines at crit or worse that nginx does not log routinely.

        Two are routine here. nginx keeps a purged entry's node in shared
        memory, and logs at crit when its cache manager later fails to unlink
        the file the purge already removed. And it alerts on an entry still in
        use when its ``inactive`` time is up, which a request waiting up to
        ``proxy_cache_lock_timeout`` for another to fill it outlasts when
        ``inactive`` is as short as the load test's; the load alone, with no
        purge, does that too.
        """
        routine = re.compile(
            r"unlink\(\) .* failed \(2: No such file or directory\)"
            r"|ignore long locked inactive cache entry"
        )
        text = self.error_log.read_text(encoding="utf-8", errors="replace")
        return [
            line
            for line in text.splitlines()
            if re.search(r"\[(?:crit|alert|emerg)\]", line) and not routine.search(line)
        ]

    def log_tail(self, lines: int = 40) -> str:
        try:
            text = self.error_log.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return "(no error log)"
        return "\n".join(text.splitlines()[-lines:])

    def keep(self, destination: Path) -> None:
        """Copy what explains a failure: configuration, logs, index, cache."""
        destination.mkdir(parents=True, exist_ok=True)
        for source in (self.conf, self.error_log, self.access_log):
            if source.exists():
                shutil.copy2(source, destination / source.name)
        for suffix in ("", "-journal", "-wal"):
            index = Path(f"{self.index}{suffix}")
            if index.exists():
                shutil.copy2(index, destination / index.name)
        listing = [
            {
                "path": str(entry.path.relative_to(self.cache)),
                "ino": entry.identity[0],
                "key": entry.key.decode("latin-1"),
                "tags": sorted(tag.decode("latin-1") for tag in entry.tags),
                "variant": entry.variant,
                "version": entry.version,
            }
            for entry in entries(self)
        ]
        (destination / "cache.json").write_text(
            json.dumps(listing, indent=2), encoding="utf-8"
        )

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(PATIENCE)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(PATIENCE)


class Entry(NamedTuple):
    """One cache file as it was read: which file, and what it held."""

    path: Path
    #: Inode, change time and modification time: a file renamed over this
    #: path later is another file, even if the kernel reuses the inode.
    identity: tuple[int, int, int]
    key: bytes
    tags: frozenset[bytes]
    variant: str
    version: int


_HEADER_END = re.compile(rb"\r?\n\r?\n")


def _read(path: Path) -> Entry | None:
    """What one cache file holds, read independently of the scanner's index."""
    try:
        with path.open("rb") as handle:
            status = os.fstat(handle.fileno())
            data = handle.read(65536)
    except FileNotFoundError:
        return None
    start = data.find(b"\nKEY: ")
    if start < 0:
        return None
    start += len(b"\nKEY: ")
    end = data.find(b"\n", start)
    header_end = _HEADER_END.search(data, end + 1)
    if end < 0 or header_end is None:
        return None
    header = data[end + 1 : header_end.start()].partition(b"\n")[2]
    tags: set[bytes] = set()
    for line in header.split(b"\n"):
        name, colon, value = line.rstrip(b"\r").partition(b":")
        if colon and name.strip().lower() == TAG_HEADER:
            tags |= {t.strip().lower() for t in value.split(b",") if t.strip()}
    body = data[header_end.end() :]
    found = re.match(rb"\S+ variant=(\S*) v=(\d+)\n", body)
    return Entry(
        path,
        (status.st_ino, status.st_ctime_ns, status.st_mtime_ns),
        data[start:end],
        frozenset(tags),
        found.group(1).decode() if found else "",
        int(found.group(2)) if found else -1,
    )


def entries(edge: Edge) -> list[Entry]:
    return [entry for path in edge.files() if (entry := _read(path)) is not None]


# ----------------------------------------------------------------------
# The purge, as the role runs it
# ----------------------------------------------------------------------


def _load_scanner() -> ModuleType:
    """The shipped scanner as a module of its own, so a test can hook into it."""
    spec = importlib.util.spec_from_file_location("purge_scan_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scanner() -> ModuleType:
    return _load_scanner()


def keys_for(edge: Edge, host: str, uri: str) -> list[str]:
    """A named purge's keys, as the role computes them for this edge's listener.

    The role's own Jinja is executed against the template's key in
    test_cache_role_contracts.py; here it is the same product over the role's
    methods and encodings, for the one port this nginx listens on.
    """
    defaults = _role_defaults()
    return [
        f"http{edge.port}{method}{host}{uri}{encoding}"
        for method in defaults["blitzecdn_cache_purge_methods"]
        for encoding in defaults["blitzecdn_cache_purge_encodings"]
    ]


def purge(
    scanner: ModuleType | None,
    edge: Edge,
    *,
    keys: tuple[str, ...] | list[str] = (),
    prefixes: tuple[dict[str, str], ...] = (),
    tags: tuple[str, ...] = (),
    everything: bool = False,
) -> dict[str, int]:
    """Run the scanner with the request the role sends it.

    ``scanner`` runs it in this process, where a test can hook into it; None
    runs the script in a process of its own, as the role does on an edge.
    """
    request = {
        "path": str(edge.cache),
        "index": str(edge.index),
        "all": everything,
        "keys": list(keys),
        "prefixes": list(prefixes),
        "tags": list(tags),
    }
    if scanner is None:
        ran = subprocess.run(  # noqa: S603 - the shipped script over a tmp_path cache
            [sys.executable, str(SCRIPT)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert ran.returncode == 0, ran.stderr
        return json.loads(ran.stdout)
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(request)), io.StringIO()
    try:
        assert scanner.main() == 0
        report = json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    return report


@contextmanager
def between_refresh_and_delete(
    scanner: ModuleType, action: Callable[[], None]
) -> Iterator[None]:
    """Run ``action`` once, after the purge has refreshed its index.

    That is the point at which the purge has decided what it will delete: the
    index has been brought up to date and the next step deletes what it
    matches. Whatever nginx does in ``action`` happens while the purge runs.
    """
    original = scanner.refresh
    done = False

    def refresh(*args: Any, **kwargs: Any) -> Any:
        nonlocal done
        result = original(*args, **kwargs)
        if not done:
            done = True
            action()
        return result

    scanner.refresh = refresh
    try:
        yield
    finally:
        scanner.refresh = original
    assert done, "the purge never refreshed its index"


@pytest.fixture
def make_edge(
    tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[Callable[..., Edge]]:
    _nginx()
    made: list[tuple[Edge, Origin]] = []

    def make(
        *, max_size: str = "64m", inactive: str = "1h", body_size: int = 256
    ) -> Edge:
        origin = Origin(body_size=body_size)
        workdir = tmp_path / f"edge{len(made)}"
        workdir.mkdir()
        try:
            edge = Edge(workdir, origin, max_size=max_size, inactive=inactive)
        except BaseException:
            origin.close()
            raise
        made.append((edge, origin))
        return edge

    yield make
    failed = any(
        getattr(getattr(request.node, f"report_{when}", None), "failed", False)
        for when in ("setup", "call")
    )
    for number, (edge, origin) in enumerate(made):
        edge.stop()
        origin.close()
        if failed:
            print(f"--- nginx error log, edge {number} ---\n{edge.log_tail()}")
            diagnostics = os.environ.get("DIAGNOSTICS_DIR")
            if diagnostics:
                name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid)
                edge.keep(
                    Path(diagnostics) / "cache-purge-nginx" / name / f"edge{number}"
                )


# ----------------------------------------------------------------------
# Named, prefix and tag purges, every Vary representation, every index state
# ----------------------------------------------------------------------

A, B = "a.test", "b.test"
#: Every URL the scenario caches, each in the primary representation (no
#: variant) and one secondary one.
CACHED = [
    (A, "/page"),
    (A, "/page?x=1"),
    (A, "/blog/one"),
    (A, "/blog/two"),
    (A, "/blogroll"),
    (B, "/blog/one"),
    (A, "/shop/42"),
    (A, "/shop/43"),
    (A, "/shop/44"),
]
VARIANTS = ("", "mobile")
TAGS = {
    "/shop/42": "product-42, listing",
    # Tags match case-insensitively.
    "/shop/43": "Product-42",
    "/shop/44": "product-420",
}
#: What each purge must remove, in order. Each removes every representation of
#: these URLs, and nothing else; a URL not listed survives to the end.
PURGED_BY = {
    "named": [(A, "/page")],
    "prefix": [(A, "/blog/one"), (A, "/blog/two")],
    "tag": [(A, "/shop/42"), (A, "/shop/43")],
}
SURVIVORS = [(A, "/page?x=1"), (A, "/blogroll"), (B, "/blog/one"), (A, "/shop/44")]


def _fill(edge: Edge) -> dict[tuple[str, str, str], int]:
    """Cache every URL in every representation, and check nginx serves them."""
    stored: dict[tuple[str, str, str], int] = {}
    for host, path in CACHED:
        for variant in VARIANTS:
            first = edge.get(host, path, variant=variant)
            assert first.cache == "MISS", (host, path, variant, first)
            stored[(host, path, variant)] = first.version
    for (host, path, variant), version in stored.items():
        assert edge.get(host, path, variant=variant) == Seen("HIT", version)
    return stored


def _outcome(edge: Edge, stored: dict[tuple[str, str, str], int]) -> dict[str, str]:
    """For each URL and representation: served from cache, or fetched again."""
    result = {}
    for (host, path, variant), version in sorted(stored.items()):
        seen = edge.get(host, path, variant=variant)
        if seen == Seen("HIT", version):
            result[f"{host}{path}#{variant}"] = "cached"
        else:
            # Gone from the cache: nginx went back to the origin.
            assert seen.cache == "MISS" and seen.version > version, seen
            result[f"{host}{path}#{variant}"] = "refetched"
    return result


EXPECTED = {
    f"{host}{path}#{variant}": ("cached" if (host, path) in SURVIVORS else "refetched")
    for host, path in CACHED
    for variant in VARIANTS
}


@pytest.mark.parametrize("index_state", ["cold", "warm", "rebuilt"])
def test_named_prefix_and_tag_purges_remove_every_representation_from_a_real_cache(
    make_edge, scanner, index_state
):
    edge = make_edge()
    edge.origin.tags_for = lambda path: TAGS.get(path.partition("?")[0], "")
    if index_state != "cold":
        # An index from an earlier purge, before most of the cache existed.
        edge.get(A, "/unrelated")
        assert purge(scanner, edge, tags=("nothing",))["removed"] == 0
    stored = _fill(edge)
    if index_state == "rebuilt":
        edge.index.write_bytes(b"not a database, " * 64)
    elif index_state == "cold":
        assert not edge.index.exists()
    files_before = len(entries(edge))
    assert files_before >= len(CACHED) * len(VARIANTS)

    reports = {
        "named": purge(scanner, edge, keys=keys_for(edge, A, "/page")),
        "prefix": purge(scanner, edge, prefixes=({"host": A, "prefix": "/blog/"},)),
        "tag": purge(scanner, edge, tags=("PRODUCT-42",)),
    }

    for kind, report in reports.items():
        assert report["unchecked"] == 0, (kind, report)
        assert report["removed"] == len(PURGED_BY[kind]) * len(VARIANTS), (kind, report)
    # Only the first purge of a cold or rebuilt index reads every file.
    # The first purge reads what its index has not seen: everything, unless
    # the index is warm and already knows the one earlier entry.
    assert reports["named"]["read"] == files_before - (index_state == "warm")
    assert reports["prefix"]["read"] == reports["tag"]["read"] == 0
    assert _outcome(edge, stored) == EXPECTED
    # Purged again with nothing new cached, each finds nothing more.
    for kind, again in (
        ("named", purge(scanner, edge, keys=keys_for(edge, A, "/page"))),
        ("prefix", purge(scanner, edge, prefixes=({"host": A, "prefix": "/blog/"},))),
        ("tag", purge(scanner, edge, tags=("product-42",))),
    ):
        # Each refetch above stored the URL again; the repeat removes that.
        assert again["unchecked"] == 0, (kind, again)
        assert again["removed"] == len(PURGED_BY[kind]) * len(VARIANTS), (kind, again)


def test_the_warm_index_describes_the_cache_a_rebuilt_one_would(make_edge, scanner):
    edge = make_edge()
    edge.origin.tags_for = lambda path: TAGS.get(path, "")
    purge(scanner, edge, tags=("nothing",))
    _fill(edge)
    purge(scanner, edge, prefixes=({"host": A, "prefix": "/blog/"},))
    edge.get(A, "/blog/one", variant="tablet")
    edge.get(A, "/page", refill=True)
    purge(scanner, edge, tags=("nothing",))

    assert _index_rows(scanner, edge, str(edge.index)) == _index_rows(
        scanner, edge, scanner.NO_INDEX
    )


def _index_rows(
    scanner: ModuleType, edge: Edge, location: str
) -> set[tuple[str, str, bytes, frozenset[bytes]]]:
    """Every file the index lists, with its key and tags, after a refresh."""
    db = scanner.open_index(location, edge.cache, TAG_HEADER)
    try:
        refreshed = scanner.refresh(db, edge.cache, TAG_HEADER)
        assert refreshed.failed == 0
        tags: dict[int, set[bytes]] = {}
        for file_id, tag in db.execute("SELECT file, tag FROM tags"):
            tags.setdefault(file_id, set()).add(tag)
        rows = {
            (directory, name, key, frozenset(tags.get(file_id, ())))
            for file_id, directory, name, key in db.execute(
                "SELECT id, dir, name, key FROM files"
            )
        }
        db.execute("ROLLBACK")
    finally:
        db.close()
    return rows


# ----------------------------------------------------------------------
# What nginx does while a purge runs
# ----------------------------------------------------------------------


def test_a_fill_that_lands_during_a_purge_may_survive_it_and_the_next_purge_removes_it(
    make_edge, scanner
):
    edge = make_edge()
    before = {variant: edge.get(A, "/late", variant=variant) for variant in VARIANTS}
    # The tablet representation is being fetched when the purge starts: the
    # origin has the request and has not answered.
    hold = edge.origin.hold(A, "/late")
    fetch = threading.Thread(
        target=edge.get, args=(A, "/late"), kwargs={"variant": "tablet"}
    )
    fetch.start()
    assert hold.arrived.wait(PATIENCE)

    def land() -> None:
        hold.release.set()
        fetch.join(PATIENCE)
        edge.wait_for_file(lambda entry: entry.variant == "tablet")

    with between_refresh_and_delete(scanner, land):
        report = purge(scanner, edge, keys=keys_for(edge, A, "/late"))

    # What was in place when the purge began is gone; the fill that landed
    # after it is not, and that is not a failure of this purge.
    assert report == {"scanned": 2, "read": 2, "removed": 2, "unchecked": 0}
    assert [entry.variant for entry in entries(edge)] == ["tablet"]
    (late,) = entries(edge)
    for variant in VARIANTS:
        seen = edge.get(A, "/late", variant=variant)
        assert seen.cache == "MISS" and seen.version > before[variant].version
    # nginx finds a secondary representation through the primary file. Once
    # a primary is stored again, the late one is served from cache: the
    # survivor is real, not just a file nobody reads.
    assert edge.get(A, "/late", variant="tablet") == Seen("HIT", late.version)

    again = purge(scanner, edge, keys=keys_for(edge, A, "/late"))

    assert again["removed"] == 3 and again["unchecked"] == 0
    for variant in (*VARIANTS, "tablet"):
        assert edge.get(A, "/late", variant=variant).cache == "MISS"


def test_an_entry_replaced_during_a_purge_goes_with_the_one_it_replaced(
    make_edge, scanner
):
    edge = make_edge()
    edge.origin.tags_for = lambda path: "listing"
    for variant in VARIANTS:
        edge.get(A, "/replaced", variant=variant)
    primary = next(entry for entry in entries(edge) if entry.variant == "")
    replacement: list[Entry] = []

    def replace() -> None:
        # nginx fetches the URL again and renames the new file over the old.
        edge.get(A, "/replaced", refill=True)
        replacement.append(
            edge.wait_for_file(
                lambda entry: (
                    entry.path == primary.path and entry.identity != primary.identity
                )
            )
        )

    with between_refresh_and_delete(scanner, replace):
        report = purge(scanner, edge, tags=("listing",))

    assert replacement[0].version > primary.version
    # The purge deleted the path it matched, whichever file was there by then.
    assert report["removed"] == 2 and report["unchecked"] == 0
    assert entries(edge) == []
    for variant in VARIANTS:
        assert edge.get(A, "/replaced", variant=variant).cache == "MISS"


def test_an_entry_evicted_during_a_purge_is_neither_removed_nor_unchecked(
    make_edge, scanner
):
    edge = make_edge(max_size="64m")
    for variant in VARIANTS:
        edge.get(A, "/evicted", variant=variant)
    secondary = next(entry for entry in entries(edge) if entry.variant == "mobile")

    def evict() -> None:
        # nginx's cache manager evicts by unlinking the file, as here.
        secondary.path.unlink()

    with between_refresh_and_delete(scanner, evict):
        report = purge(scanner, edge, prefixes=({"host": A, "prefix": "/evicted"},))

    assert report["removed"] == 1 and report["unchecked"] == 0
    for variant in VARIANTS:
        assert edge.get(A, "/evicted", variant=variant).cache == "MISS"


# ----------------------------------------------------------------------
# A purge that cannot finish says so
# ----------------------------------------------------------------------


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root reads and deletes regardless of permissions",
)
def test_a_file_the_purge_cannot_delete_or_read_leaves_it_incomplete_and_served(
    make_edge, scanner
):
    edge = make_edge()
    edge.origin.tags_for = lambda path: "stuck"
    stored = {variant: edge.get(A, "/stuck", variant=variant) for variant in VARIANTS}
    held = {entry.variant: entry for entry in entries(edge)}
    directory = held[""].path.parent

    directory.chmod(0o555)
    try:
        report = purge(scanner, edge, tags=("stuck",))
        # The role fails the edge on this, so the purge is not complete.
        assert report["unchecked"] >= 1
        assert held[""].path.exists()
        # And it is still being served, which is why the purge must not
        # report success.
        assert edge.get(A, "/stuck") == Seen("HIT", stored[""].version)
        again = purge(scanner, edge, tags=("stuck",))
        assert again["unchecked"] >= 1
    finally:
        directory.chmod(0o700)

    done = purge(scanner, edge, tags=("stuck",))
    assert done["unchecked"] == 0 and done["removed"] >= 1
    assert edge.get(A, "/stuck").cache == "MISS"

    # A file nginx stored that cannot be read is not a non-match either.
    edge.get(A, "/unreadable")
    unreadable = next(e for e in entries(edge) if b"/unreadable" in e.key)
    unreadable.path.chmod(0o000)
    try:
        report = purge(scanner, edge, keys=keys_for(edge, A, "/other"))
        assert report["unchecked"] == 1 and report["removed"] == 0
        # The index does not trust that directory until the file is read.
        assert purge(scanner, edge, keys=keys_for(edge, A, "/other"))["unchecked"] == 1
    finally:
        unreadable.path.chmod(0o600)
    readable = purge(scanner, edge, keys=keys_for(edge, A, "/unreadable"))
    assert readable["removed"] == 1 and readable["unchecked"] == 0


def test_a_file_with_no_key_is_reported_on_every_purge_until_it_is_gone(
    make_edge, scanner
):
    edge = make_edge()
    edge.get(A, "/page")
    level = next(iter(edge.files())).parent
    stray = level / ("0" * 32)
    stray.write_bytes(b"not a cache file\n")

    for _attempt in range(2):
        report = purge(scanner, edge, keys=keys_for(edge, A, "/page"))
        assert report["unchecked"] == 1
        assert stray.exists()
    edge.get(A, "/page")

    emptied = purge(scanner, edge, everything=True)

    assert emptied["unchecked"] == 0 and emptied["removed"] == 2
    assert purge(scanner, edge, keys=keys_for(edge, A, "/page"))["unchecked"] == 0
    assert edge.get(A, "/page").cache == "MISS"


# ----------------------------------------------------------------------
# Purging while nginx fills, replaces and evicts at full speed
# ----------------------------------------------------------------------

LOAD_HOSTS = (A, B)
LOAD_PATHS = tuple(f"/p/{number}" for number in range(24))
LOAD_VARIANTS = ("", "mobile", "tablet")


#: The scheme, port and method that lead every key; the hostname follows.
KEY_LEAD = re.compile(rb"^https?\d+[A-Z]+")


@dataclass(frozen=True)
class Selection:
    """A purge to run under load, and which cache files it must remove."""

    name: str
    request: dict[str, Any]
    keys: frozenset[bytes] = frozenset()
    target: bytes | None = None
    tag: bytes | None = None

    def matches(self, entry: Entry) -> bool:
        if self.target is not None:
            return KEY_LEAD.sub(b"", entry.key).startswith(self.target)
        if self.tag is not None:
            return self.tag in entry.tags
        return entry.key in self.keys


def _selections(edge: Edge) -> list[Selection]:
    named = [
        Selection(
            f"named {uri}",
            {"keys": keys_for(edge, A, uri)},
            keys=frozenset(key.encode() for key in keys_for(edge, A, uri)),
        )
        for uri in ("/p/3", "/p/7", "/p/11")
    ]
    prefixed = [
        Selection(
            f"prefix {prefix}",
            {"prefixes": ({"host": A, "prefix": prefix},)},
            target=f"{A}{prefix}".encode(),
        )
        for prefix in ("/p/1", "/p/2")
    ]
    tagged = [
        Selection(f"tag {tag}", {"tags": (tag,)}, tag=tag.encode())
        for tag in ("t0", "t3")
    ]
    return [*named, *prefixed, *tagged]


def test_purges_under_concurrent_fills_replacements_and_eviction(make_edge, scanner):
    # Room for a few dozen of the 144 representations, and two seconds of
    # inactivity before an entry goes, so nginx's cache manager evicts all the
    # while: for max_size and for inactivity.
    edge = make_edge(max_size="160k", inactive="2s", body_size=3000)
    edge.start_evicting()
    edge.origin.tags_for = lambda path: f"t{int(path.rsplit('/', 1)[1]) % 5}, all"
    stop = threading.Event()
    errors: list[BaseException] = []
    requests = Counter[str]()

    def load(seed: int) -> None:
        chooser = random.Random(seed)  # noqa: S311 - a load pattern, not a secret
        # One connection per worker: a connection per request runs a laptop
        # out of ephemeral ports within seconds.
        with closing(edge.connect()) as connection:
            while not stop.is_set():
                try:
                    edge.get(
                        chooser.choice(LOAD_HOSTS),
                        chooser.choice(LOAD_PATHS),
                        variant=chooser.choice(LOAD_VARIANTS),
                        refill=chooser.random() < 0.1,
                        connection=connection,
                    )
                    requests["ok"] += 1
                except BaseException as error:  # noqa: BLE001 - reported below
                    errors.append(error)
                    return

    workers = [threading.Thread(target=load, args=(seed,)) for seed in range(4)]
    for worker in workers:
        worker.start()
    try:
        # Before any purge, so only nginx removes anything: cache files whose
        # path is gone were evicted. A replaced file keeps its path.
        seen: set[Path] = set()
        evicted: set[Path] = set()
        deadline = time.monotonic() + PATIENCE
        while time.monotonic() < deadline and not errors and len(evicted) < 10:
            present = set(edge.files())
            evicted |= seen - present
            seen |= present
            time.sleep(0.05)
        assert errors == [], errors
        assert len(evicted) >= 10, "nginx never evicted"

        selections = _selections(edge)
        for _round in range(2):
            for selection in selections:
                # Twice in a row: the second finds only what landed since.
                for _repeat in range(2):
                    before = [e for e in entries(edge) if selection.matches(e)]
                    # In a process of its own, as on an edge: in this one it
                    # would contend with the load for the interpreter.
                    report = purge(None, edge, **selection.request)
                    assert report["unchecked"] == 0, (selection.name, report)
                    survivors = [
                        entry
                        for entry in before
                        if (now := _read(entry.path)) is not None
                        and now.identity == entry.identity
                    ]
                    assert survivors == [], (selection.name, report, survivors)
    finally:
        stop.set()
        for worker in workers:
            worker.join(PATIENCE)
    assert errors == [], errors
    assert requests["ok"] > 100
    # nginx keeps a purged entry's node in shared memory and logs at crit when
    # its cache manager later fails to unlink the file the purge removed.
    # That is expected; anything else that serious is not.
    assert edge.serious_errors() == []

    # The load has stopped. A purge of the whole hostname leaves nothing of it
    # in the cache, or for nginx to serve.
    final = purge(None, edge, prefixes=({"host": A, "prefix": "/"},))
    assert final["unchecked"] == 0
    hostname = re.compile(rb"^https?\d+[A-Z]+" + re.escape(f"{A}/".encode()))
    assert not any(hostname.match(entry.key) for entry in entries(edge))
    for path in LOAD_PATHS[:6]:
        for variant in LOAD_VARIANTS:
            assert edge.get(A, path, variant=variant).cache == "MISS"
    # And the index all those purges kept warm describes the cache as one
    # rebuilt from nothing does.
    _assert_index_matches_a_rebuild(scanner, edge)


def _assert_index_matches_a_rebuild(scanner: ModuleType, edge: Edge) -> None:
    """Compare the warm index with a rebuilt one, while nginx holds still.

    The cache manager goes on evicting what the load left. Two indexes read a
    moment apart can differ by what it removed in between, so the comparison
    counts only when the cache was the same before and after both.
    """
    for _attempt in range(50):
        before = {path: status.st_ino for path, status in edge.files().items()}
        warm = _index_rows(scanner, edge, str(edge.index))
        rebuilt = _index_rows(scanner, edge, scanner.NO_INDEX)
        after = {path: status.st_ino for path, status in edge.files().items()}
        if before == after:
            assert warm == rebuilt
            assert len(warm) == len(before) > 0
            return
    raise AssertionError("the cache never held still long enough to compare")
