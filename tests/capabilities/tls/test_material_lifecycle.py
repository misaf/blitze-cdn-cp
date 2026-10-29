"""Canonical resource versions survive renewal, replay and retained history."""

import pytest
from support.application import seed_proxied_record
from support.certificates import certificate_control_plane
from support.control_plane import FakeRunner

from blitzecdn.capabilities.releases.service.compiler import compile_release
from blitzecdn.capabilities.releases.service.preparation import prepare_compilation
from blitzecdn.capabilities.tls.certificates.adapters import CertificateStore
from blitzecdn.capabilities.tls.certificates.domain import CertificateSource
from blitzecdn.capabilities.tls.contract import (
    CertificateVersion,
    managed_certificate_paths,
)
from blitzecdn.core.exceptions import ConfigurationError


def _upload(control, certificate_pair, *, days=60):
    certificate, key = certificate_pair(days=days)
    return control.certificates.upload_certificate(
        "example-com", certificate, key, "alice"
    )


def test_compilation_replays_canonical_versions_after_metadata_moves(
    settings, certificate_pair
):
    control = certificate_control_plane(settings, runner=FakeRunner())
    seed_proxied_record(control)
    first = _upload(control, certificate_pair, days=40)
    captured = control.releases.capture()
    original = compile_release(captured)
    original_site = control.sites.get_site("example-com")
    second = _upload(control, certificate_pair, days=80)

    assert first.version != second.version
    assert captured.inputs.digest != control.releases.inputs().digest
    assert (
        original_site.policy.tls.certificate_path
        == managed_certificate_paths(first.site, first.version)[0]
    )
    service = control.releases
    replayed = compile_release(
        prepare_compilation(
            captured.inputs,
            capabilities=service.capabilities,
            targets=captured.targets,
            contributors=service.contributors,
            site_checks=service.site_checks,
            allow_empty_sites=service.allow_empty_sites,
        )
    )
    assert replayed.digest == original.digest
    sources = control.certificates.installed_sources(original_site)
    assert sources[0].name == f"fullchain-{first.certificate_digest}.pem"


def test_collection_preserves_retained_release_and_metadata_then_reclaims_after_pruning(
    settings, certificate_pair
):
    control = certificate_control_plane(settings, runner=FakeRunner())
    seed_proxied_record(control)
    first = _upload(control, certificate_pair, days=40)
    old = control.releases.prepare()
    second = _upload(control, certificate_pair, days=80)
    store = CertificateStore(settings)
    old_files = store.sources(first.site, version=first.version)
    newest_files = store.sources(second.site)
    assert control.certificates.collect_material("alice") == ()
    assert all(path.exists() for path in (*old_files, *newest_files))

    control.releases.prepare()
    control.releases.releases.prune(1)
    assert old.id not in {
        release.id for release in control.releases.list_releases(limit=None)
    }
    removed = control.certificates.collect_material("alice")
    assert removed
    assert all(not path.exists() for path in old_files if path not in newest_files)
    assert all(path.exists() for path in newest_files)
    assert control.certificates.collect_material("alice") == ()
    assert (
        control.audit.list_audit_events()[0].action == "certificate.material.collected"
    )


def test_collection_reads_all_retained_releases_not_only_the_recent_page(
    settings, certificate_pair
):
    control = certificate_control_plane(settings, runner=FakeRunner())
    seed_proxied_record(control)
    first = _upload(control, certificate_pair, days=40)
    control.releases.prepare()
    # Keep the old material only through history, beyond the default page size.
    _upload(control, certificate_pair, days=80)
    from blitzecdn.capabilities.releases.domain import Release

    inputs = control.releases.inputs()
    for index in range(25):
        control.releases.releases.save(
            Release(inputs_digest=inputs.digest, capabilities=(str(index),)), inputs
        )
    assert len(control.releases.list_releases(limit=None)) == 26
    control.certificates.collect_material("alice")
    assert all(
        path.exists()
        for path in CertificateStore(settings).sources(
            first.site, version=first.version
        )
    )


def test_canonical_reference_protects_material_even_without_metadata_or_a_release(
    settings, certificate_pair
):
    control = certificate_control_plane(settings, runner=FakeRunner())
    seed_proxied_record(control)
    first = _upload(control, certificate_pair)
    paths = CertificateStore(settings).sources(first.site)
    (settings.certificate_dir / first.site / "metadata.json").unlink()
    assert control.certificates.collect_material("alice") == ()
    assert all(path.exists() for path in paths)
    assert control.releases.compile().sites


def test_collection_preserves_a_pair_installed_before_failed_canonical_writeback(
    settings, certificate_pair
):
    control = certificate_control_plane(settings, runner=FakeRunner())
    seed_proxied_record(control)
    first = _upload(control, certificate_pair, days=40)
    site = control.sites.get_site(first.site)
    certificate, key = certificate_pair(days=80)
    store = CertificateStore(settings)
    pending = store.install(site, certificate, key, source=CertificateSource.UPLOADED)
    assert pending.version != first.version
    assert control.certificates.collect_material("alice") == ()
    assert store.sources(site.name, version=first.version)
    assert store.sources(site.name, version=pending.version)


def test_inactive_rule_override_remains_a_material_root(settings, certificate_pair):
    from support.control_plane import seed_site

    from blitzecdn.capabilities.dns.domain import RulePatch

    control = certificate_control_plane(settings, runner=FakeRunner())
    site = seed_site(control, name="rule-host", record="api")
    certificate, key = certificate_pair(("api.example.com",))
    info = control.certificates.upload_certificate(site.name, certificate, key, "alice")
    files = control.certificates.installed_sources(control.sites.get_site(site.name))
    (settings.certificate_dir / site.name / "metadata.json").unlink()
    control.rules.update_rule(
        "example.com", "api", RulePatch(match="unused.example.com"), "alice"
    )
    assert all(host.name != site.name for host in control.sites.list_sites())
    assert control.certificates.collect_material("alice") == ()
    assert all(path.exists() for path in files)
    assert info.certificate_digest in files[0].name


def test_damaged_retained_inputs_abort_collection_before_any_deletion(
    settings, certificate_pair
):
    from sqlalchemy import select

    from blitzecdn.capabilities.releases.adapters.tables import ReleaseInputsRow

    control = certificate_control_plane(settings, runner=FakeRunner())
    seed_proxied_record(control)
    _upload(control, certificate_pair)
    control.releases.prepare()
    orphan = settings.certificate_dir / "orphan" / f"fullchain-{'a' * 64}.pem"
    orphan.parent.mkdir()
    orphan.write_bytes(b"orphan")
    with control.transactions.database.session() as session:
        row = session.scalars(select(ReleaseInputsRow)).one()
        row.document = (
            control.releases.inputs().model_copy(update={"domains": ()}).encode()
        )
        session.add(row)
    with pytest.raises(ConfigurationError, match="integrity"):
        control.certificates.collect_material("alice")
    assert orphan.exists()


def test_interrupted_collection_can_be_retried(settings, monkeypatch):
    from pathlib import Path

    directory = settings.certificate_dir / "orphan"
    directory.mkdir(parents=True)
    files = [directory / f"fullchain-{digest * 64}.pem" for digest in ("a", "b")]
    for path in files:
        path.write_bytes(b"orphan")
    unlink = Path.unlink

    def fail_second(path, *args, **kwargs):
        if path == files[1]:
            raise OSError("interrupted")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_second)
        with pytest.raises(OSError, match="interrupted"):
            CertificateStore(settings).collect(())
    assert not files[0].exists()
    assert CertificateStore(settings).collect(()) == (f"orphan/{files[1].name}",)


def test_collector_fails_closed_on_corrupt_metadata_before_deleting_anything(settings):
    root = settings.certificate_dir
    first = root / "a"
    first.mkdir(parents=True)
    orphan = first / f"fullchain-{'a' * 64}.pem"
    orphan.write_bytes(b"unreferenced")
    damaged = root / "z"
    damaged.mkdir()
    (damaged / "metadata.json").write_text("broken")
    with pytest.raises(ValueError):
        CertificateStore(settings).collect(())
    assert orphan.exists()


def test_collector_ignores_external_links_and_unknown_files(settings, tmp_path):
    root = settings.certificate_dir
    directory = root / "example-com"
    directory.mkdir(parents=True)
    external = tmp_path / "external.pem"
    external.write_bytes(b"external")
    linked = directory / f"privkey-{'a' * 64}.pem"
    linked.symlink_to(external)
    unknown = directory / "fullchain.pem"
    unknown.write_bytes(b"unmanaged")
    (root / "linked-directory").symlink_to(directory, target_is_directory=True)
    assert CertificateStore(settings).collect(()) == ()
    assert external.read_bytes() == b"external"
    assert linked.is_symlink()
    assert unknown.exists()


def test_selected_missing_or_corrupt_material_never_falls_back_to_latest(
    settings, certificate_pair
):
    control = certificate_control_plane(settings, runner=FakeRunner())
    seed_proxied_record(control)
    first = _upload(control, certificate_pair, days=40)
    site = control.sites.get_site(first.site)
    path = control.certificates.installed_sources(site)[0]
    _upload(control, certificate_pair, days=80)
    path.write_bytes(b"damaged")
    with pytest.raises(ConfigurationError, match="integrity"):
        control.certificates.installed_sources(site)
    path.unlink()
    with pytest.raises(ConfigurationError, match="missing"):
        control.certificates.installed_sources(site)


def test_captured_checksums_cannot_follow_a_file_changed_after_verification(
    settings, certificate_pair, monkeypatch
):
    control = certificate_control_plane(settings, runner=FakeRunner())
    seed_proxied_record(control)
    info = _upload(control, certificate_pair)
    site = control.sites.get_site(info.site)
    sources = control.certificate_store.sources

    def tampered_after_verification(site_name, *, version=None):
        files = sources(site_name, version=version)
        files[0].write_bytes(b"changed after verification")
        return files

    monkeypatch.setattr(
        control.certificate_store, "sources", tampered_after_verification
    )
    variables = control.rendering.site_variables(site)
    assert variables["certificate_source_sha256"] == info.certificate_digest
    assert variables["certificate_key_source_sha256"] == info.private_key_digest


def test_deployment_history_keeps_material_and_rollback_restores_its_version(
    settings, certificate_pair
):
    from blitzecdn.capabilities.deployments.domain import DeploymentStatus

    control = certificate_control_plane(settings, runner=FakeRunner())
    seed_proxied_record(control)
    first = _upload(control, certificate_pair, days=40)
    deployed = control.deployments.deploy("alice")
    assert deployed.status is DeploymentStatus.SUCCEEDED
    original_digest = control.releases.inputs().digest
    _upload(control, certificate_pair, days=80)
    control.releases.prepare()
    control.releases.releases.prune(1)
    control.certificates.collect_material("alice")
    assert CertificateStore(settings).sources(first.site, version=first.version)

    restored = control.deployments.rollback("alice", deployed.id)
    assert restored.status is DeploymentStatus.SUCCEEDED
    assert control.releases.inputs().digest == original_digest
    assert (
        control.sites.get_site(first.site).policy.tls.certificate_path
        == managed_certificate_paths(first.site, first.version)[0]
    )
    assert (
        control.releases.compile().digest
        == control.releases.get(deployed.release_id).digest
    )


def test_collection_holds_the_write_boundary_while_reading_roots_and_deleting(
    settings, monkeypatch
):
    control = certificate_control_plane(settings, runner=FakeRunner())
    database = control.transactions.database
    service = control.certificates
    seen = []

    def references():
        assert database.current_session() is not None
        seen.append(database.current_session())
        return ()

    def collect(paths):
        assert database.current_session() is seen[0]
        return ()

    monkeypatch.setattr(service, "material_references", references)
    monkeypatch.setattr(service.persistence.certificates, "collect", collect)
    assert service.collect_material("alice") == ()


def test_cli_collect_reports_reclaimed_files(settings, monkeypatch):
    import json

    from support.certificates import certificate_cli_control_plane
    from typer.testing import CliRunner

    from blitzecdn.entrypoints.cli import main as cli

    certificate_cli_control_plane(settings, monkeypatch)
    directory = settings.certificate_dir / "orphan"
    directory.mkdir(parents=True)
    orphan = directory / f"fullchain-{'a' * 64}.pem"
    orphan.write_bytes(b"unreferenced")
    result = CliRunner().invoke(cli.app, ["cert", "collect", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"removed": [f"orphan/{orphan.name}"]}
    assert not orphan.exists()


@pytest.mark.parametrize(
    "certificate,key",
    [
        ("fullchain.pem", "privkey.pem"),
        (f"fullchain-{'a' * 64}.pem", f"../other/privkey-{'b' * 64}.pem"),
    ],
)
def test_managed_version_refuses_mutable_or_cross_site_paths(certificate, key):
    with pytest.raises(ValueError):
        CertificateVersion.from_paths(
            "example-com",
            f"/etc/blitzecdn/tls/example-com/{certificate}",
            f"/etc/blitzecdn/tls/example-com/{key}",
        )
