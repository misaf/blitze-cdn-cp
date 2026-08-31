"""Backup and restore, from the component rules up to a full round trip.

The archive is a compatibility contract with the operator's future self, so the
cases here are weighted towards what an archive is *allowed to be*: what it may
contain, what it must contain, and what a restore must refuse before it has
written anything. A backup that fails loudly is recoverable; one that restores
the wrong thing quietly is not.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tarfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import FakeRunner
from typer.testing import CliRunner

from blitzecdn.bootstrap import ControlPlane, build_backup_service
from blitzecdn.cli import main as cli
from blitzecdn.core.database import Repository
from blitzecdn.core.exceptions import ConfigurationError, ExecutionError, NotFoundError
from blitzecdn.features.backup.adapters import (
    AlembicSchemaVersions,
    TarArchive,
    TemporaryWorkspace,
)
from blitzecdn.features.backup.adapters.components import DATABASE_MEMBER
from blitzecdn.features.backup.adapters.services import ComposeRestoreGuard
from blitzecdn.features.backup.domain import (
    BACKUP_FORMAT_VERSION,
    BackupComponent,
    backup_filename,
    member_component,
    parse_manifest,
    unsafe_member,
)
from blitzecdn.features.backup.service import BackupPolicy, BackupService
from blitzecdn.features.dns.domain import DnsRecord, Domain, RecordType

runner = CliRunner()


# --- fixtures ---------------------------------------------------------


def _populate(settings, *, database=True, tls=True, acme=True, config=True) -> None:
    """Give a controller the state a disaster-recovery backup has to capture."""
    if database:
        store = Repository(settings.database_path)
        control = ControlPlane(settings=settings, repository=store, runner=FakeRunner())  # type: ignore[arg-type]
        control.dns.create_domain(Domain(name="example.com"), operator="tester")
        store.close()
    if tls:
        directory = settings.certificate_dir / "cdn-example-com"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "fullchain-aa.pem").write_text("CERTIFICATE", encoding="utf-8")
        (directory / "privkey-aa.pem").write_text("PRIVATE KEY", encoding="utf-8")
        (directory / "metadata.json").write_text("{}", encoding="utf-8")
    if acme:
        root = settings.state_dir / "letsencrypt/config"
        (root / "accounts/acme-v02").mkdir(parents=True, exist_ok=True)
        (root / "accounts/acme-v02/private_key.json").write_text("{}", encoding="utf-8")
        (root / "archive/cdn-example-com").mkdir(parents=True, exist_ok=True)
        (root / "archive/cdn-example-com/fullchain1.pem").write_text(
            "CHAIN", encoding="utf-8"
        )
        (root / "live/cdn-example-com").mkdir(parents=True, exist_ok=True)
        link = root / "live/cdn-example-com/fullchain.pem"
        if not link.is_symlink():
            link.symlink_to("../../archive/cdn-example-com/fullchain1.pem")
    if config:
        (settings.project_dir / "blitzecdn.toml").write_text(
            "[blitzecdn]\n", encoding="utf-8"
        )
        (settings.project_dir / ".env").write_text(
            f"BLITZE_API_KEYS=operator:{'k' * 32}\n", encoding="utf-8"
        )
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        (settings.state_dir / "id_ed25519").write_text("PRIVATE", encoding="utf-8")
        (settings.state_dir / "id_ed25519.pub").write_text("PUBLIC", encoding="utf-8")


@pytest.fixture
def populated(settings):
    _populate(settings)
    return settings


@pytest.fixture
def service(populated):
    return build_backup_service(populated)


def _members(archive: Path) -> list[str]:
    with tarfile.open(archive) as handle:
        return sorted(member.name for member in handle.getmembers())


def _manifest(archive: Path) -> dict:
    with tarfile.open(archive) as handle:
        stream = handle.extractfile("manifest.json")
        assert stream is not None
        return json.loads(stream.read())


def _repack(source: Path, destination: Path, edit) -> Path:
    """Rebuild an archive after a callback has tampered with its contents.

    Archives under test are built by the real writer and then modified, rather
    than assembled by hand: a test that constructs its own tar can only assert
    against the shape it invented.
    """
    staging = destination.parent / f"{destination.stem}-staging"
    with tarfile.open(source) as handle:
        handle.extractall(staging, filter="data")
    edit(staging)
    with tarfile.open(destination, "w:gz") as handle:
        for path in sorted(staging.rglob("*")):
            handle.add(
                path,
                arcname=path.relative_to(staging).as_posix(),
                recursive=False,
            )
    return destination


# --- the domain rules -------------------------------------------------
@pytest.mark.parametrize(
    "name",
    ["/etc/passwd", "../escape", "database/../../etc/passwd", "", "C:/windows"],
)
def test_unsafe_members_are_named_and_refused(name):
    assert unsafe_member(name) is not None


def test_links_of_either_kind_are_refused():
    assert unsafe_member("tls/key.pem", is_link=True) is not None
    assert unsafe_member("tls/key.pem", link_target="../../etc/shadow") is not None
    assert unsafe_member("tls/key.pem") is None


def test_the_filename_is_readable_utc(populated):
    from datetime import UTC, datetime

    moment = datetime(2026, 8, 30, 0, 15, 30, tzinfo=UTC)
    assert backup_filename(moment) == "blitzecdn-backup-2026-08-30_00-15-30Z.tar.gz"


# --- creating ---------------------------------------------------------


def test_a_full_backup_takes_every_component_that_exists(service, populated):
    archive = service.create()
    assert archive.parent == populated.backup_dir
    assert _manifest(archive)["components"] == ["acme", "config", "database", "tls"]


def test_a_full_backup_skips_a_component_with_nothing_in_it(settings):
    """A controller that has never issued a certificate still has a backup."""
    _populate(settings, tls=False, acme=False)
    archive = build_backup_service(settings).create()
    assert _manifest(archive)["components"] == ["config", "database"]


def test_database_only(service):
    archive = service.create(only=(BackupComponent.DATABASE,))
    assert _manifest(archive)["components"] == ["database"]
    assert [name for name in _members(archive) if name.startswith("tls")] == []


def test_tls_only(service):
    archive = service.create(only=(BackupComponent.TLS,))
    assert _manifest(archive)["components"] == ["tls"]
    assert "database" not in _members(archive)
    # No database means no schema to record, and recording the running one
    # would let a restore reject an archive that holds no database at all.
    assert _manifest(archive)["database_schema_version"] is None


def test_several_selected_components(service):
    archive = service.create(only=(BackupComponent.DATABASE, BackupComponent.TLS))
    assert _manifest(archive)["components"] == ["database", "tls"]
    assert "acme" not in _members(archive)


def test_duplicate_selected_components_are_refused(service):
    with pytest.raises(ConfigurationError, match="must not repeat"):
        service.create(only=(BackupComponent.TLS, BackupComponent.TLS))


def test_selecting_a_component_with_no_data_is_an_error(settings):
    _populate(settings, tls=False)
    with pytest.raises(NotFoundError):
        build_backup_service(settings).create(only=(BackupComponent.TLS,))


def test_the_filename_and_directory_are_the_defaults(service, populated):
    archive = service.create()
    assert archive.parent == populated.backup_dir
    assert archive.name.startswith("blitzecdn-backup-")
    assert archive.name.endswith("Z.tar.gz")


def test_a_custom_output_path_is_used(service, populated):
    target = populated.state_dir / "export" / "controller.tar.gz"
    assert service.create(destination=target) == target
    assert target.is_file()


def test_the_archive_is_private_and_so_is_its_directory(service, populated):
    archive = service.create()
    assert archive.stat().st_mode & 0o777 == 0o600
    assert populated.backup_dir.stat().st_mode & 0o777 == 0o700


def test_the_manifest_records_format_version_and_schema_separately(service):
    document = _manifest(service.create())
    assert document["format_version"] == BACKUP_FORMAT_VERSION
    assert document["database_schema_version"] not in {None, BACKUP_FORMAT_VERSION}
    assert document["created_at"].endswith("Z")
    assert document["blitzecdn_version"]


def test_the_manifest_matches_what_the_archive_actually_holds(service):
    archive = service.create()
    declared = set(_manifest(archive)["components"])
    found = {
        name.split("/", 1)[0] for name in _members(archive) if name != "manifest.json"
    }
    assert declared == found


def test_the_database_copy_is_consistent_and_readable(service, populated):
    """`VACUUM INTO`, not `cp` — the copy must stand alone without a `-wal`."""
    archive = service.create(only=(BackupComponent.DATABASE,))
    with tarfile.open(archive) as handle:
        handle.extractall(populated.state_dir / "unpacked", filter="data")
    copy = populated.state_dir / "unpacked/database" / DATABASE_MEMBER
    assert not copy.with_name(copy.name + "-wal").exists()
    connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT name FROM domains").fetchall() == [
            ("example.com",)
        ]
    finally:
        connection.close()


def test_certificates_and_their_private_keys_are_both_included(service):
    names = _members(service.create(only=(BackupComponent.TLS,)))
    assert "tls/cdn-example-com/fullchain-aa.pem" in names
    assert "tls/cdn-example-com/privkey-aa.pem" in names


def test_config_includes_the_controller_ssh_identity(service):
    names = _members(service.create(only=(BackupComponent.CONFIG,)))
    assert "config/controller_key" in names
    assert "config/controller_key.pub" in names


def test_acme_account_state_is_included_when_selected(service):
    names = _members(service.create(only=(BackupComponent.ACME,)))
    assert "acme/accounts/acme-v02/private_key.json" in names
    # Certbot's live/ tree is symlinks. They are recorded rather than stored as
    # tar links, because this project never extracts a link.
    assert "acme/.links.json" in names


def test_no_member_carries_an_absolute_path_or_a_link(service):
    archive = service.create()
    with tarfile.open(archive) as handle:
        for member in handle.getmembers():
            assert not member.name.startswith("/")
            assert ".." not in member.name.split("/")
            assert not member.issym() and not member.islnk()
            # Ownership is dropped: a backup is restored onto a host whose
            # uids differ from this one's.
            assert member.uid == 0 and member.uname == ""


def test_an_existing_destination_is_never_overwritten(service, populated):
    target = populated.backup_dir / "taken.tar.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"precious")
    with pytest.raises(ConfigurationError, match="refusing to overwrite"):
        service.create(destination=target)
    assert target.read_bytes() == b"precious"


def test_a_failed_creation_leaves_no_temporary_files(populated, monkeypatch):
    service = build_backup_service(populated)

    def explode(_self, _staging: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(
        type(service._components[BackupComponent.DATABASE]), "export", explode
    )
    with pytest.raises(OSError, match="disk full"):
        service.create()
    assert list(populated.backup_dir.glob("*")) == []
    assert list((populated.state_dir / "backup-work").glob("*")) == []


def test_a_destination_race_never_overwrites_and_cleans_the_temporary_file(
    service, populated, monkeypatch
):
    target = populated.backup_dir / "raced.tar.gz"

    def lose_race(_source, destination):
        Path(destination).write_bytes(b"winner")
        raise FileExistsError

    monkeypatch.setattr(os, "link", lose_race)
    with pytest.raises(ConfigurationError, match="refusing to overwrite"):
        service.create(destination=target)
    assert target.read_bytes() == b"winner"
    assert list(target.parent.glob(f".{target.name}.*")) == []


# --- restoring --------------------------------------------------------


def _clean(settings) -> None:
    """Wipe the controller's state the way a rebuilt host would arrive."""
    for path in (
        settings.database_path,
        settings.project_dir / ".env",
        settings.project_dir / "blitzecdn.toml",
        settings.state_dir / "id_ed25519",
        settings.state_dir / "id_ed25519.pub",
    ):
        path.unlink(missing_ok=True)
    for directory in (
        settings.certificate_dir,
        settings.state_dir / "letsencrypt/config",
    ):
        if directory.exists():
            for child in sorted(directory.rglob("*"), reverse=True):
                child.unlink() if not child.is_dir() else child.rmdir()


def test_a_full_backup_restores_every_component(populated):
    archive = build_backup_service(populated).create()
    _clean(populated)
    manifest = build_backup_service(populated).restore(archive)
    assert [component.value for component in manifest.components] == [
        "acme",
        "config",
        "database",
        "tls",
    ]
    assert populated.database_path.is_file()
    assert (populated.certificate_dir / "cdn-example-com/privkey-aa.pem").is_file()
    assert (populated.project_dir / ".env").is_file()
    assert (
        populated.state_dir / "letsencrypt/config/accounts/acme-v02/private_key.json"
    ).is_file()


def test_a_database_only_backup_restores_only_the_database(populated):
    archive = build_backup_service(populated).create(only=(BackupComponent.DATABASE,))
    (populated.certificate_dir / "cdn-example-com/privkey-aa.pem").write_text(
        "REPLACED", encoding="utf-8"
    )
    populated.database_path.unlink()
    manifest = build_backup_service(populated).restore(archive)
    assert [c.value for c in manifest.components] == ["database"]
    assert populated.database_path.is_file()
    # Untouched, because the manifest never mentioned it.
    assert (
        populated.certificate_dir / "cdn-example-com/privkey-aa.pem"
    ).read_text() == "REPLACED"


def test_a_tls_only_restore_does_nothing_database_shaped(populated):
    archive = build_backup_service(populated).create(only=(BackupComponent.TLS,))
    before = populated.database_path.read_bytes()
    _clean(populated)
    populated.database_path.write_bytes(before)
    service = build_backup_service(populated)
    service.services = _RecordingServices()
    service.restore(archive)
    assert (populated.certificate_dir / "cdn-example-com/privkey-aa.pem").is_file()
    assert populated.database_path.read_bytes() == before
    # TLS is read by the deploy that follows, never held open, so a restore of
    # it must not take the controller offline.
    assert service.services.calls == []


def test_multiple_selected_components_restore_together(populated):
    archive = build_backup_service(populated).create(
        only=(BackupComponent.DATABASE, BackupComponent.TLS)
    )
    _clean(populated)
    build_backup_service(populated).restore(archive)
    assert populated.database_path.is_file()
    assert (populated.certificate_dir / "cdn-example-com/fullchain-aa.pem").is_file()
    assert not (populated.project_dir / ".env").exists()


def test_the_manifest_decides_what_is_restored(populated, tmp_path):
    """Data present in the archive but absent from the manifest is refused.

    The alternative — restoring whatever directories happen to be there —
    would let a tampered archive add a component the manifest never declared.
    """
    archive = build_backup_service(populated).create(only=(BackupComponent.DATABASE,))
    tampered = _repack(
        archive,
        tmp_path / "tampered.tar.gz",
        lambda staging: (
            (staging / "tls").mkdir()
            or (staging / "tls/privkey-evil.pem").write_text("EVIL", encoding="utf-8")
        ),
    )
    with pytest.raises(ConfigurationError, match="does not declare"):
        build_backup_service(populated).restore(tampered)


def test_a_newer_format_version_is_refused(populated, tmp_path):
    archive = build_backup_service(populated).create(only=(BackupComponent.TLS,))

    def bump(staging: Path) -> None:
        path = staging / "manifest.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["format_version"] = BACKUP_FORMAT_VERSION + 1
        path.write_text(json.dumps(document), encoding="utf-8")

    tampered = _repack(archive, tmp_path / "newer.tar.gz", bump)
    with pytest.raises(ConfigurationError, match="newer than this installation"):
        build_backup_service(populated).restore(tampered)


def test_an_unknown_component_is_refused_by_name(populated, tmp_path):
    archive = build_backup_service(populated).create(only=(BackupComponent.TLS,))

    def add(staging: Path) -> None:
        path = staging / "manifest.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["components"] = ["tls", "quantum"]
        path.write_text(json.dumps(document), encoding="utf-8")

    tampered = _repack(archive, tmp_path / "unknown.tar.gz", add)
    with pytest.raises(ConfigurationError, match="unknown components: quantum"):
        build_backup_service(populated).restore(tampered)


def test_a_declared_component_with_no_data_is_refused(populated, tmp_path):
    archive = build_backup_service(populated).create(only=(BackupComponent.TLS,))

    def declare(staging: Path) -> None:
        path = staging / "manifest.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["components"] = ["database", "tls"]
        document["database_schema_version"] = "0001"
        path.write_text(json.dumps(document), encoding="utf-8")

    tampered = _repack(archive, tmp_path / "missing.tar.gz", declare)
    with pytest.raises(ConfigurationError, match="does not contain"):
        build_backup_service(populated).restore(tampered)


def test_a_component_missing_its_required_file_is_refused(populated, tmp_path):
    archive = build_backup_service(populated).create(only=(BackupComponent.DATABASE,))

    def corrupt(staging: Path) -> None:
        (staging / "database" / DATABASE_MEMBER).write_text("nope", encoding="utf-8")

    tampered = _repack(archive, tmp_path / "corrupt.tar.gz", corrupt)
    with pytest.raises(ConfigurationError, match="not a SQLite database"):
        build_backup_service(populated).restore(tampered)


def _handmade(destination: Path, build) -> Path:
    with tarfile.open(destination, "w:gz") as handle:
        build(handle)
    return destination


def test_path_traversal_is_refused(populated, tmp_path):
    payload = tmp_path / "payload"
    payload.write_text("owned", encoding="utf-8")

    def build(handle: tarfile.TarFile) -> None:
        handle.add(payload, arcname="manifest.json")
        handle.add(payload, arcname="../../etc/passwd")

    archive = _handmade(tmp_path / "traversal.tar.gz", build)
    with pytest.raises(ConfigurationError, match="escapes its root"):
        build_backup_service(populated).restore(archive)


def test_an_absolute_member_is_refused(populated, tmp_path):
    payload = tmp_path / "payload"
    payload.write_text("owned", encoding="utf-8")

    def build(handle: tarfile.TarFile) -> None:
        info = handle.gettarinfo(str(payload), arcname="etc/shadow")
        # Set after `gettarinfo`, which strips the leading slash. `tar -P`
        # does not, and that is the archive this has to refuse.
        info.name = "/etc/shadow"
        with payload.open("rb") as stream:
            handle.addfile(info, stream)

    archive = _handmade(tmp_path / "absolute.tar.gz", build)
    with pytest.raises(ConfigurationError, match="absolute path"):
        build_backup_service(populated).restore(archive)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_a_link_member_is_refused(populated, tmp_path, kind):
    payload = tmp_path / "payload"
    payload.write_text("{}", encoding="utf-8")

    def build(handle: tarfile.TarFile) -> None:
        handle.add(payload, arcname="manifest.json")
        info = tarfile.TarInfo("tls/privkey.pem")
        info.type = kind
        info.linkname = "manifest.json" if kind == tarfile.LNKTYPE else "/etc/shadow"
        handle.addfile(info)

    archive = _handmade(tmp_path / f"link-{kind.decode()}.tar.gz", build)
    with pytest.raises(ConfigurationError, match="contains a link"):
        build_backup_service(populated).restore(archive)


def test_a_special_file_member_is_refused(populated, tmp_path):
    def build(handle: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("tls/fifo")
        info.type = tarfile.FIFOTYPE
        handle.addfile(info)

    archive = _handmade(tmp_path / "fifo.tar.gz", build)
    with pytest.raises(ConfigurationError, match="special file"):
        build_backup_service(populated).restore(archive)


def test_duplicate_archive_members_are_refused(populated, tmp_path):
    payload = tmp_path / "payload"
    payload.write_text("{}", encoding="utf-8")

    def build(handle: tarfile.TarFile) -> None:
        handle.add(payload, arcname="manifest.json")
        handle.add(payload, arcname="manifest.json")

    archive = _handmade(tmp_path / "duplicate.tar.gz", build)
    with pytest.raises(ConfigurationError, match="duplicate entry"):
        build_backup_service(populated).restore(archive)


def test_an_archive_without_a_manifest_is_refused(populated, tmp_path):
    payload = tmp_path / "payload"
    payload.write_text("{}", encoding="utf-8")
    archive = _handmade(
        tmp_path / "bare.tar.gz", lambda handle: handle.add(payload, arcname="tls/x")
    )
    with pytest.raises(ConfigurationError, match=r"no manifest\.json"):
        build_backup_service(populated).restore(archive)


def test_a_file_that_is_not_an_archive_is_refused(populated, tmp_path):
    archive = tmp_path / "not-a-backup.tar.gz"
    archive.write_bytes(b"this is not a tar file")
    with pytest.raises(ConfigurationError, match="not a readable backup"):
        build_backup_service(populated).restore(archive)


def test_a_missing_archive_is_not_found(populated, tmp_path):
    with pytest.raises(NotFoundError):
        build_backup_service(populated).restore(tmp_path / "absent.tar.gz")


def test_a_schema_this_installation_does_not_know_is_refused(populated, tmp_path):
    """A database from a later release is never migrated backwards."""
    archive = build_backup_service(populated).create(only=(BackupComponent.DATABASE,))

    def forward(staging: Path) -> None:
        path = staging / "manifest.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["database_schema_version"] = "9999_from_the_future"
        path.write_text(json.dumps(document), encoding="utf-8")

    tampered = _repack(archive, tmp_path / "future.tar.gz", forward)
    with pytest.raises(ConfigurationError, match="does not know"):
        build_backup_service(populated).restore(tampered)


def test_the_staged_database_revision_must_match_the_manifest(populated, tmp_path):
    archive = build_backup_service(populated).create(only=(BackupComponent.DATABASE,))

    def remove_revision(staging: Path) -> None:
        database = staging / "database" / DATABASE_MEMBER
        connection = sqlite3.connect(database)
        try:
            connection.execute("DELETE FROM alembic_version")
            connection.commit()
        finally:
            connection.close()

    tampered = _repack(archive, tmp_path / "schema-mismatch.tar.gz", remove_revision)
    service = build_backup_service(populated)
    service.services = _RecordingServices()
    with pytest.raises(ConfigurationError, match="does not match the manifest"):
        service.restore(tampered)
    assert service.services.calls == []


def test_validation_happens_before_anything_is_modified(populated, tmp_path):
    """A bad component in an otherwise good archive changes nothing at all."""
    archive = build_backup_service(populated).create(
        only=(BackupComponent.DATABASE, BackupComponent.TLS)
    )
    key = populated.certificate_dir / "cdn-example-com/privkey-aa.pem"
    key.write_text("STILL HERE", encoding="utf-8")
    database = populated.database_path.read_bytes()

    def corrupt(staging: Path) -> None:
        (staging / "database" / DATABASE_MEMBER).write_text("nope", encoding="utf-8")

    tampered = _repack(archive, tmp_path / "half-bad.tar.gz", corrupt)
    service = build_backup_service(populated)
    service.services = _RecordingServices()
    with pytest.raises(ConfigurationError):
        service.restore(tampered)
    assert key.read_text() == "STILL HERE"
    assert populated.database_path.read_bytes() == database
    assert service.services.calls == []


def test_restored_tls_private_keys_are_not_world_readable(populated):
    archive = build_backup_service(populated).create(only=(BackupComponent.TLS,))
    key = populated.certificate_dir / "cdn-example-com/privkey-aa.pem"
    key.chmod(0o644)
    build_backup_service(populated).restore(archive)
    assert key.stat().st_mode & 0o777 == 0o600
    assert key.parent.stat().st_mode & 0o777 == 0o700


def test_a_restored_database_is_consistent_and_migrated(populated):
    archive = build_backup_service(populated).create(only=(BackupComponent.DATABASE,))
    populated.database_path.unlink()
    build_backup_service(populated).restore(archive)
    store = Repository(populated.database_path)
    control = ControlPlane(settings=populated, repository=store, runner=FakeRunner())  # type: ignore[arg-type]
    try:
        assert [domain.name for domain in control.dns.list_domains()] == ["example.com"]
    finally:
        store.close()
    assert AlembicSchemaVersions.of(populated.database_path) is not None


def test_an_acme_link_that_escapes_is_refused_before_restore(populated, tmp_path):
    archive = build_backup_service(populated).create(only=(BackupComponent.ACME,))

    def escape(staging: Path) -> None:
        links = staging / "acme/.links.json"
        links.write_text(
            json.dumps({"live/cdn-example-com/fullchain.pem": "../../../../outside"}),
            encoding="utf-8",
        )

    tampered = _repack(archive, tmp_path / "escaping-acme.tar.gz", escape)
    service = build_backup_service(populated)
    service.services = _RecordingServices()
    with pytest.raises(ConfigurationError, match="escapes its component"):
        service.restore(tampered)
    assert service.services.calls == []


class _RecordingServices:
    """A stand-in for systemd that remembers what it was asked to do."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @contextmanager
    def stopped(self):
        self.calls.append("stop")
        try:
            yield
        finally:
            self.calls.append("start")


def test_services_come_back_even_when_a_restore_fails(populated, monkeypatch):
    archive = build_backup_service(populated).create(only=(BackupComponent.DATABASE,))
    service = build_backup_service(populated)
    service.services = _RecordingServices()

    def explode(_self, _staging: Path) -> None:
        raise OSError("the disk went away")

    monkeypatch.setattr(
        type(service._components[BackupComponent.DATABASE]), "restore", explode
    )
    with pytest.raises(OSError, match="the disk went away"):
        service.restore(archive)
    assert service.services.calls == ["stop", "start"]


def test_a_database_restore_takes_the_controller_offline(populated):
    archive = build_backup_service(populated).create(only=(BackupComponent.DATABASE,))
    service = build_backup_service(populated)
    service.services = _RecordingServices()
    service.restore(archive)
    assert service.services.calls == ["stop", "start"]


# --- round trip -------------------------------------------------------


def _relocated(settings, root: Path):
    """The same settings pointed at a host that has just been installed."""
    return settings.model_copy(
        update={
            "project_dir": root,
            "state_dir": root / ".state",
            "database_path": root / ".state/control-plane.db",
            "certificate_dir": root / ".state/certificates",
            "environment_path": root / ".env",
            "backup_dir": root / ".state/backups",
            "generated_vars_path": root / ".state/desired-state.yml",
            "deployment_lock_path": root / ".state/deployment.lock",
        }
    )


def test_a_full_backup_rebuilds_a_controller_from_nothing(settings, tmp_path):
    """Create state, back it up, install fresh, restore, and check it works.

    The final assertion is the one that matters: the restored controller renders
    byte-for-byte the same edge configuration the original would have. Generated
    edge state is never archived, so this is what proves the *authoritative*
    state came back intact and can regenerate the rest.
    """
    _populate(settings)
    store = Repository(settings.database_path)
    original = ControlPlane(settings=settings, repository=store, runner=FakeRunner())  # type: ignore[arg-type]
    original.dns.create_record(
        DnsRecord(
            domain="example.com",
            name="cdn",
            type=RecordType.A,
            value="198.51.100.10",
            proxied=True,
        ),
        operator="tester",
    )
    expected = tmp_path / "expected.yml"
    original.deployments.write_desired_state(store.deployments.snapshot(), expected)
    store.close()
    archive = build_backup_service(settings).create()

    fresh = tmp_path / "fresh-install"
    fresh.mkdir()
    rebuilt = _relocated(settings, fresh)
    manifest = build_backup_service(rebuilt).restore(archive)
    assert set(manifest.components) == set(BackupComponent)

    store = Repository(rebuilt.database_path)
    control = ControlPlane(settings=rebuilt, repository=store, runner=FakeRunner())  # type: ignore[arg-type]
    try:
        assert [domain.name for domain in control.dns.list_domains()] == ["example.com"]
        rendered = tmp_path / "rendered.yml"
        control.deployments.write_desired_state(store.deployments.snapshot(), rendered)
    finally:
        store.close()
    assert rendered.read_text(encoding="utf-8") == expected.read_text(encoding="utf-8")
    assert (rebuilt.certificate_dir / "cdn-example-com/privkey-aa.pem").read_text() == (
        "PRIVATE KEY"
    )
    assert (rebuilt.project_dir / ".env").read_text().startswith("BLITZE_API_KEYS=")
    assert (rebuilt.state_dir / "id_ed25519").read_text() == "PRIVATE"
    link = rebuilt.state_dir / "letsencrypt/config/live/cdn-example-com/fullchain.pem"
    assert link.is_symlink() and link.read_text() == "CHAIN"


def test_a_selected_round_trip_restores_only_what_was_taken(settings, tmp_path):
    _populate(settings)
    archive = build_backup_service(settings).create(only=(BackupComponent.TLS,))
    fresh = tmp_path / "tls-only"
    fresh.mkdir()
    rebuilt = _relocated(settings, fresh)
    build_backup_service(rebuilt).restore(archive)
    assert (rebuilt.certificate_dir / "cdn-example-com/fullchain-aa.pem").is_file()
    assert not rebuilt.database_path.exists()
    assert not (rebuilt.project_dir / ".env").exists()


def test_config_restore_preserves_fresh_machine_specific_settings(settings, tmp_path):
    settings.environment_path.write_text(
        "BLITZE_API_KEYS=old:" + "o" * 32 + "\nBLITZE_REDIS_URL=redis://old/0\n",
        encoding="utf-8",
    )
    (settings.project_dir / "blitzecdn.toml").write_text(
        "[blitzecdn]\n"
        'database_path = "/old/controller.db"\n'
        'backup_dir = "/old/backups"\n'
        'environment_path = "/old/blitzecdn.env"\n'
        'redis_url = "redis://old/0"\n'
        "deployment_timeout_seconds = 1200\n",
        encoding="utf-8",
    )
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "id_ed25519").write_text("PRIVATE", encoding="utf-8")
    (settings.state_dir / "id_ed25519.pub").write_text("PUBLIC", encoding="utf-8")
    archive = build_backup_service(settings).create(only=(BackupComponent.CONFIG,))

    fresh = _relocated(settings, tmp_path / "fresh-config")
    fresh.project_dir.mkdir()
    fresh.environment_path.write_text(
        "BLITZE_API_KEYS=fresh:" + "f" * 32 + "\nBLITZE_REDIS_URL=redis://fresh/0\n",
        encoding="utf-8",
    )
    (fresh.project_dir / "blitzecdn.toml").write_text(
        "[blitzecdn]\n"
        'database_path = ".state/fresh.db"\n'
        'backup_dir = "/fresh/backups"\n'
        'environment_path = ".env"\n'
        'redis_url = "redis://fresh/0"\n'
        "deployment_timeout_seconds = 900\n",
        encoding="utf-8",
    )
    build_backup_service(fresh).restore(archive)

    restored = (fresh.project_dir / "blitzecdn.toml").read_text(encoding="utf-8")
    assert 'database_path = ".state/fresh.db"' in restored
    assert 'backup_dir = "/fresh/backups"' in restored
    assert 'redis_url = "redis://fresh/0"' in restored
    assert "deployment_timeout_seconds = 1200" in restored
    environment = fresh.environment_path.read_text(encoding="utf-8")
    assert "BLITZE_API_KEYS=old:" in environment
    assert "BLITZE_REDIS_URL=redis://fresh/0" in environment


# --- the archive adapter ----------------------------------------------


def test_the_writer_refuses_to_archive_a_symlink(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "real").write_text("x", encoding="utf-8")
    (staging / "link").symlink_to("real")
    with pytest.raises(ConfigurationError, match="refusing to archive a symlink"):
        TarArchive().write(staging, tmp_path / "out.tar.gz")


def test_the_workspace_removes_its_directory_however_it_is_left(tmp_path):
    workspace = TemporaryWorkspace(tmp_path / "work")
    with pytest.raises(RuntimeError), workspace.scratch("t-") as scratch:
        created = scratch
        raise RuntimeError("boom")
    assert not created.exists()


def test_a_backup_service_without_a_component_refuses_to_restore_it(
    populated, tmp_path
):
    """Forward extensibility, from the other side.

    An installation that has never heard of a component must say so rather than
    skip it — a restore that quietly leaves out half the archive is the worst
    possible answer.
    """
    archive = build_backup_service(populated).create(only=(BackupComponent.TLS,))
    partial = BackupService(
        policy=BackupPolicy(backup_dir=populated.backup_dir, version="0"),
        components=(),
        archive=TarArchive(),
        schema=AlembicSchemaVersions(populated),
        services=_RecordingServices(),
        workspace=TemporaryWorkspace(tmp_path / "work"),
    )
    with pytest.raises(ConfigurationError, match="cannot restore: tls"):
        partial.restore(archive)


# --- the command line -------------------------------------------------


@pytest.fixture
def cli_settings(populated, monkeypatch):
    monkeypatch.setattr(cli.common, "settings", lambda: populated)
    return populated


def test_cli_create_reports_where_the_backup_landed(cli_settings):
    result = runner.invoke(cli.app, ["backup", "create"])
    assert result.exit_code == 0
    written = result.stdout.split("Backup created: ")[1].strip()
    assert Path(written).is_file()
    assert Path(written).parent == cli_settings.backup_dir


def test_cli_create_only_selects_components(cli_settings):
    result = runner.invoke(
        cli.app,
        ["backup", "create", "--only", "database", "--only", "tls"],
    )
    assert result.exit_code == 0
    archive = Path(result.stdout.split("Backup created: ")[1].strip())
    assert _manifest(archive)["components"] == ["database", "tls"]


def test_cli_create_accepts_three_repeated_selections(cli_settings):
    result = runner.invoke(
        cli.app,
        [
            "backup",
            "create",
            "--only",
            "database",
            "--only",
            "tls",
            "--only",
            "acme",
        ],
    )
    assert result.exit_code == 0
    archive = Path(result.stdout.split("Backup created: ")[1].strip())
    assert _manifest(archive)["components"] == ["acme", "database", "tls"]


def test_cli_create_rejects_comma_separated_selection(cli_settings):
    result = runner.invoke(cli.app, ["backup", "create", "--only", "database,tls"])
    assert result.exit_code != 0
    assert "database,tls" in result.output


def test_cli_create_rejects_duplicate_selections(cli_settings):
    result = runner.invoke(
        cli.app,
        ["backup", "create", "--only", "tls", "--only", "tls"],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigurationError)
    assert "must not repeat" in str(result.exception)


def test_cli_create_rejects_an_unknown_component(cli_settings):
    result = runner.invoke(cli.app, ["backup", "create", "--only", "sekrets"])
    assert result.exit_code != 0


def test_cli_inspect_describes_without_revealing(cli_settings):
    archive = Path(
        runner.invoke(cli.app, ["backup", "create"])
        .stdout.split("Backup created: ")[1]
        .strip()
    )
    result = runner.invoke(cli.app, ["backup", "inspect", str(archive)])
    assert result.exit_code == 0
    assert "Components:" in result.stdout
    assert "  database" in result.stdout
    # It says what is in there, never any of it.
    assert "PRIVATE" not in result.stdout


def test_cli_restore_needs_confirmation_and_says_what_it_did(cli_settings):
    archive = Path(
        runner.invoke(cli.app, ["backup", "create", "--only", "tls"])
        .stdout.split("Backup created: ")[1]
        .strip()
    )
    declined = runner.invoke(cli.app, ["backup", "restore", str(archive)], input="n\n")
    assert declined.exit_code != 0
    accepted = runner.invoke(cli.app, ["backup", "restore", str(archive), "--yes"])
    assert accepted.exit_code == 0
    assert "Restored: tls" in accepted.stdout


def test_the_legacy_database_backup_command_is_not_registered(cli_settings):
    result = runner.invoke(cli.app, ["db", "backup"])
    assert result.exit_code != 0
    assert "No such command 'db'" in result.output


def test_the_empty_legacy_database_group_is_not_registered(cli_settings):
    result = runner.invoke(cli.app, ["db"])
    assert result.exit_code != 0
    assert "No such command 'db'" in result.output


# --- the domain manifest rules ----------------------------------------


def test_a_manifest_with_no_components_is_refused():
    with pytest.raises(ConfigurationError):
        parse_manifest(
            {
                "format_version": 1,
                "created_at": "2026-08-30T00:15:30Z",
                "blitzecdn_version": "2.6.1",
                "components": [],
            }
        )


def test_a_manifest_that_repeats_a_component_is_refused():
    with pytest.raises(ConfigurationError, match="must not repeat"):
        parse_manifest(
            {
                "format_version": 1,
                "created_at": "2026-08-30T00:15:30Z",
                "blitzecdn_version": "2.6.1",
                "components": ["tls", "tls"],
            }
        )


def test_a_manifest_that_is_not_an_object_is_refused():
    with pytest.raises(ConfigurationError, match="not a JSON object"):
        parse_manifest(["database"])


def test_an_unrecognised_manifest_field_is_ignored():
    """Forward extensibility: a later release may record more than this one.

    `format_version` is what guards compatibility. A field this release has no
    use for must not turn a readable archive into an unreadable one.
    """
    manifest = parse_manifest(
        {
            "format_version": 1,
            "created_at": "2026-08-30T00:15:30Z",
            "blitzecdn_version": "9.9.9",
            "components": ["tls"],
            "something_from_the_future": {"nested": True},
        }
    )
    assert manifest.components == (BackupComponent.TLS,)


def test_an_archive_entry_outside_a_component_is_refused():
    with pytest.raises(ConfigurationError, match="unexpected entry"):
        member_component("notes/readme.txt")
    assert member_component("manifest.json") is None
    assert member_component("tls/key.pem") is BackupComponent.TLS


# --- the Compose restore boundary ------------------------------------


def test_container_restore_requires_the_host_compose_wrapper(monkeypatch):
    monkeypatch.setattr(
        "blitzecdn.features.backup.adapters.services.Path.exists", lambda _path: True
    )
    monkeypatch.delenv("COMPOSE_RESTORE_OFFLINE", raising=False)
    with (
        pytest.raises(ExecutionError, match="must run through the host"),
        ComposeRestoreGuard().stopped(),
    ):
        pytest.fail("the restore body must not run")


def test_container_restore_accepts_the_offline_boundary(monkeypatch):
    monkeypatch.setattr(
        "blitzecdn.features.backup.adapters.services.Path.exists", lambda _path: True
    )
    monkeypatch.setenv("COMPOSE_RESTORE_OFFLINE", "1")
    with ComposeRestoreGuard().stopped():
        pass


def test_checkout_restore_needs_no_container_lifecycle(monkeypatch):
    monkeypatch.setattr(
        "blitzecdn.features.backup.adapters.services.Path.exists", lambda _path: False
    )
    monkeypatch.delenv("COMPOSE_RESTORE_OFFLINE", raising=False)
    with ComposeRestoreGuard().stopped():
        pass
