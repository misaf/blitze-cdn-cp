"""The controller downloads GeoLite2 once and every release pins that file.

Edges hold no MaxMind credential and never call MaxMind: a refresh replaces the
controller's database, raises a deployment requirement, and the next release
carries the file's path and digest for the edge role to copy and verify.
"""

from __future__ import annotations

import base64
import hashlib
import io
import tarfile
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest
from pydantic import SecretStr

from blitzecdn.capabilities.security.geoip.adapters import (
    GeoIpDatabaseStore,
    MaxMindDownload,
    maxmind,
)
from blitzecdn.capabilities.security.geoip.domain import (
    DATABASE_SHA256_VARIABLE,
    EDITION,
    database_filename,
    digest_of,
    require_database,
)
from blitzecdn.capabilities.security.geoip.service import GeoIpService
from blitzecdn.capabilities.security.geoip.settings import CONFIGURATION, GeoIpConfig
from blitzecdn.capabilities.security.geoip.wiring import (
    build_geoip_service,
    geoip_operations,
    geoip_rendering,
)
from blitzecdn.core.exceptions import ConfigurationError, ExecutionError
from blitzecdn.core.features.resolution import resolve_capability_environment

MARKER = b"\xab\xcd\xefMaxMind.com"


def database(seed: bytes) -> bytes:
    """Bytes that pass for a MaxMind DB: data, then the metadata marker."""
    return seed * 64 + MARKER + b"metadata"


class Source:
    def __init__(self, *payloads: bytes) -> None:
        self.payloads = list(payloads)
        self.calls = 0

    def download(self) -> bytes:
        self.calls += 1
        return self.payloads.pop(0)


class Lock:
    def __init__(self) -> None:
        self.held = 0

    def lock(self) -> Any:
        self.held += 1
        return nullcontext()


class Requirement:
    def __init__(self) -> None:
        self.raised = 0

    def __call__(self) -> None:
        self.raised += 1


class Artifact:
    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document


class Release:
    def __init__(self, sha256: str | None) -> None:
        document = {} if sha256 is None else {DATABASE_SHA256_VARIABLE: sha256}
        self.artifacts = (Artifact(document),)


class Releases:
    def __init__(self, *releases: Release) -> None:
        self.releases = list(releases)

    def list_releases(self, limit: int | None = 20) -> list[Release]:
        assert limit is None, "every retained release, or a rollback loses its file"
        return self.releases


def service(
    tmp_path: Path, source: Source | None, releases: Releases | None = None
) -> tuple[GeoIpService, GeoIpDatabaseStore, Requirement, Lock]:
    store = GeoIpDatabaseStore(tmp_path / "geoip")
    requirement = Requirement()
    lock = Lock()
    return (
        GeoIpService(
            store=store,
            source=source,
            lock=lock,
            require_deployment=requirement,
            releases=releases or Releases(),
        ),
        store,
        requirement,
        lock,
    )


# --- the database itself ------------------------------------------------------


def test_only_a_maxmind_database_is_accepted():
    """An error page or a truncated transfer must never reach every edge."""
    payload = database(b"a")
    assert require_database(payload) == hashlib.sha256(payload).hexdigest()
    for refused in (b"", b"<html>rate limited</html>", payload[: len(payload) // 2]):
        with pytest.raises(ValueError):
            require_database(refused)


def test_a_database_is_named_by_its_digest():
    digest = "ab" * 32
    name = database_filename(digest)
    assert name == f"{EDITION}-{digest}.mmdb"
    assert digest_of(name) == digest
    assert digest_of("current") is None
    with pytest.raises(ValueError):
        database_filename("../../etc/passwd")


# --- the controller's store -------------------------------------------------


def test_the_store_keeps_immutable_files_and_points_at_the_newest(tmp_path):
    store = GeoIpDatabaseStore(tmp_path)
    assert store.current() is None

    first = store.install(database(b"a"))
    second = store.install(database(b"b"))

    assert store.current() == second
    assert first.path.read_bytes() == database(b"a")
    assert second.path.name == database_filename(second.sha256)

    assert store.collect(frozenset({second.sha256})) == 1
    assert not first.path.exists()
    assert store.current() == second


def test_the_store_refuses_what_is_not_a_database(tmp_path):
    store = GeoIpDatabaseStore(tmp_path)
    with pytest.raises(ConfigurationError, match="not a MaxMind database"):
        store.install(b"<html></html>")
    assert store.current() is None


def test_a_pointer_to_a_missing_file_says_how_to_recover(tmp_path):
    store = GeoIpDatabaseStore(tmp_path)
    store.install(database(b"a")).path.unlink()
    with pytest.raises(ConfigurationError, match="blitzecdn geoip update"):
        store.current()


# --- the refresh ------------------------------------------------------------


def test_a_new_database_is_owed_to_the_fleet(tmp_path):
    geoip, store, requirement, lock = service(tmp_path, Source(database(b"a")))

    result = geoip.refresh()

    assert result.configured and result.changed
    assert result.sha256 == store.current().sha256
    assert requirement.raised == 1
    assert lock.held == 1


def test_an_unchanged_database_costs_no_deployment(tmp_path):
    geoip, _store, requirement, _lock = service(
        tmp_path, Source(database(b"a"), database(b"a"))
    )

    geoip.refresh()
    again = geoip.refresh()

    assert not again.changed
    assert requirement.raised == 1


def test_a_database_a_retained_release_pins_survives_collection(tmp_path):
    """A rollback ships the file its release pinned, so that file must remain."""
    pinned = hashlib.sha256(database(b"a")).hexdigest()
    geoip, store, _requirement, _lock = service(
        tmp_path,
        Source(database(b"a"), database(b"b"), database(b"c")),
        Releases(Release(pinned), Release(None)),
    )

    geoip.refresh()
    geoip.refresh()
    result = geoip.refresh()

    kept = sorted(path.name for path in (tmp_path / "geoip").glob("*.mmdb"))
    assert kept == sorted(
        {database_filename(pinned), database_filename(store.current().sha256)}
    )
    assert result.removed == 1


def test_without_credentials_nothing_is_downloaded(tmp_path):
    geoip, _store, requirement, _lock = service(tmp_path, None)

    result = geoip.refresh()

    assert not geoip.configured
    assert not result.configured and not result.changed
    assert requirement.raised == 0


# --- MaxMind ----------------------------------------------------------------


def archive(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class Response(io.BytesIO):
    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def download() -> MaxMindDownload:
    return MaxMindDownload(
        account_id=SecretStr("123456"), license_key=SecretStr("SENTINELKEY")
    )


def test_the_download_never_hands_the_key_to_a_redirect(monkeypatch):
    """MaxMind redirects to a presigned URL that must not see the credential."""
    payload = database(b"a")
    seen: list[Any] = []

    def urlopen(request, timeout):
        seen.append(request)
        return Response(archive({f"{EDITION}_20261106/{EDITION}.mmdb": payload}))

    monkeypatch.setattr(maxmind, "urlopen", urlopen)

    assert download().download() == payload
    (request,) = seen
    assert request.full_url.startswith("https://download.maxmind.com/")
    expected = base64.b64encode(b"123456:SENTINELKEY").decode()
    assert request.unredirected_hdrs["Authorization"] == f"Basic {expected}"
    assert "Authorization" not in request.headers


def test_rejected_credentials_are_a_configuration_error(monkeypatch):
    def urlopen(request, timeout):
        raise HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO())

    monkeypatch.setattr(maxmind, "urlopen", urlopen)
    with pytest.raises(ConfigurationError, match="BLITZE_MAXMIND_LICENSE_KEY"):
        download().download()


def test_a_spent_daily_limit_is_reported_as_such(monkeypatch):
    def urlopen(request, timeout):
        raise HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO())

    monkeypatch.setattr(maxmind, "urlopen", urlopen)
    with pytest.raises(ExecutionError, match="daily download limit"):
        download().download()


def test_an_archive_without_the_database_is_refused(monkeypatch):
    monkeypatch.setattr(
        maxmind,
        "urlopen",
        lambda request, timeout: Response(archive({"README.txt": b"hello"})),
    )
    with pytest.raises(ExecutionError, match=f"no {EDITION}.mmdb"):
        download().download()


# --- configuration, rendering and scheduling ------------------------------------


def test_the_maxmind_credentials_never_reach_ansible(tmp_path):
    """Only the controller downloads; no playbook is ever handed the key."""
    resolved = resolve_capability_environment(
        (CONFIGURATION,),
        {
            "BLITZE_MAXMIND_ACCOUNT_ID": SecretStr("123456"),
            "BLITZE_MAXMIND_LICENSE_KEY": SecretStr("SENTINELKEY"),
        },
        {},
        tmp_path,
    )

    assert resolved.environment == {}
    config = resolved.for_feature("geoip").as_model(GeoIpConfig)
    assert config.credentials_configured


def test_a_release_pins_the_current_database_by_digest_alone(tmp_path):
    """No controller path: a controller restored elsewhere renders the same release."""
    store = GeoIpDatabaseStore(tmp_path)
    rendering = geoip_rendering(store=store)

    assert rendering.fleet_state(()) is None

    installed = store.install(database(b"a"))
    contribution = rendering.fleet_state(())

    assert contribution.variables == {DATABASE_SHA256_VARIABLE: installed.sha256}


def test_the_refresh_is_scheduled_only_with_credentials(tmp_path):
    store = GeoIpDatabaseStore(tmp_path)
    collaborators = {
        "store": store,
        "lock": Lock(),
        "require_deployment": Requirement(),
        "releases": Releases(),
    }
    idle = GeoIpConfig()
    armed = GeoIpConfig(
        account_id=SecretStr("123456"), license_key=SecretStr("SENTINELKEY")
    )

    without = geoip_operations(
        geoip=build_geoip_service(config=idle, **collaborators), config=idle
    )
    assert without.scheduled_jobs == ()

    (job,) = geoip_operations(
        geoip=build_geoip_service(config=armed, **collaborators), config=armed
    ).scheduled_jobs
    assert job.name == "geoip-refresh"
    assert job.interval_seconds == 86_400


def test_a_refresh_repairs_a_current_pointer_whose_file_was_lost(tmp_path):
    """`blitzecdn geoip update` is what the store's error tells an operator to run."""
    geoip, store, _requirement, _lock = service(
        tmp_path, Source(database(b"a"), database(b"a"))
    )
    geoip.refresh()
    store.current().path.unlink()
    with pytest.raises(ConfigurationError):
        store.current()

    repaired = geoip.refresh()

    assert repaired.sha256 == store.current().sha256
    assert store.current().path.read_bytes() == database(b"a")


def test_the_store_lives_where_backups_look_for_it(settings):
    """A restored controller must still hold every database a release pins."""
    from blitzecdn.capabilities.backup.adapters import GeoIpDatabasesComponent
    from blitzecdn.capabilities.security.geoip.wiring import build_geoip_store

    store = build_geoip_store(state_dir=settings.state_dir)
    installed = store.install(database(b"a"))
    component = GeoIpDatabasesComponent(settings)

    assert component.recovery_paths() == (installed.path.parent,)
    assert component.present()
