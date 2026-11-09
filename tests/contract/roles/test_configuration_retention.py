"""Release collection against the exact helper and real filesystem operations."""

import base64
import fcntl
import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from support.paths import CORE_ANSIBLE

_HELPER = CORE_ANSIBLE / "roles/blitzecdn_config_release/files/configuration_release.py"
ConfigurationRelease = runpy.run_path(str(_HELPER))["ConfigurationRelease"]


def release(root, number):
    candidate = ConfigurationRelease(root, f"{number:064x}", ())
    candidate.candidate.mkdir(parents=True)
    (candidate.candidate / "nginx.conf").write_text("events {}\nhttp {}\n")
    candidate.seal_candidate()
    return candidate


def deploy(root, number, keep=2):
    candidate = release(root, number)
    candidate.activate()
    candidate.commit(keep)
    return candidate


def trees(root):
    return {p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink()}


@pytest.mark.parametrize("keep", [0, 2, 5])
def test_many_commits_bound_trees_and_journals_without_pinning_the_chain(
    tmp_path, keep
):
    for number in range(1, 13):
        current = deploy(tmp_path, number, keep)
        # Explicit times make ordering independent of filesystem clock resolution.
        os.utime(current.journal, ns=(number, number))
    expected = {f"{number:064x}" for number in range(11 - keep, 13)}
    assert trees(tmp_path) == expected
    assert len(list(tmp_path.glob(".*.transaction.json"))) == len(expected)
    assert current.pointer() == current.digest
    current.verify()
    ConfigurationRelease(tmp_path, f"{11:064x}", ()).verify()


def test_commit_retry_keeps_retention_order_and_is_idempotent(tmp_path):
    for number in range(1, 6):
        current = deploy(tmp_path, number)
    before = current.journal.read_bytes(), current.journal.stat().st_mtime_ns
    current.commit(0)
    current.commit(0)
    assert (current.journal.read_bytes(), current.journal.stat().st_mtime_ns) == before
    assert trees(tmp_path) == {f"{n:064x}" for n in (4, 5)}


@pytest.mark.parametrize("switch_pointer", [False, True])
def test_pending_journal_protects_candidate_and_previous_across_crashes(
    tmp_path, switch_pointer
):
    previous = deploy(tmp_path, 1)
    pending = release(tmp_path, 2)
    pending.save_journal(
        {"status": "pending", "previous": previous.digest, "records": {}}
    )
    if switch_pointer:
        pending.switch(pending.digest)
    for number in range(3, 7):
        current = deploy(tmp_path, number, 0)
    assert pending.candidate.exists()
    assert previous.candidate.exists()
    assert pending.journal.exists()
    assert current.pointer() == current.digest


def test_replayable_rollback_survives_then_is_collected_after_supersession(tmp_path):
    previous = deploy(tmp_path, 1)
    withdrawn = release(tmp_path, 2)
    withdrawn.activate()
    withdrawn.rollback()
    previous.commit(0)
    withdrawn.rollback()
    assert withdrawn.candidate.exists()
    following = deploy(tmp_path, 3, 0)
    assert not withdrawn.candidate.exists()
    assert not withdrawn.journal.exists()
    assert following.pointer() == following.digest


@pytest.mark.parametrize(
    "bad",
    [
        "{",
        "[]",
        "{}",
        '{"status":"unknown","previous":null}',
        '{"status":"pending","previous":"../outside"}',
    ],
)
def test_invalid_journal_defers_all_cleanup_without_failing_commit(
    tmp_path, capsys, bad
):
    for number in range(1, 5):
        current = deploy(tmp_path, number, 20)
    (tmp_path / f".{9:064x}.transaction.json").write_text(bad)
    before = trees(tmp_path)
    current.commit(0)
    assert trees(tmp_path) == before
    assert current.read_journal()["status"] == "committed"
    assert "cleanup deferred" in capsys.readouterr().err


def test_collection_failure_is_retryable_after_a_durable_commit(
    tmp_path, monkeypatch, capsys
):
    for number in range(1, 5):
        current = deploy(tmp_path, number, 20)
    rmtree = runpy.run_path(str(_HELPER))["shutil"].rmtree

    def interrupted(path):
        # Simulate a crash/error after removing the tree but before its journal.
        rmtree(path)
        raise OSError("disk failure")

    with monkeypatch.context() as patch:
        patch.setattr("shutil.rmtree", interrupted)
        current.commit(0)
    assert current.read_journal()["status"] == "committed"
    assert "disk failure" in capsys.readouterr().err
    current.commit(0)
    assert trees(tmp_path) == {f"{n:064x}" for n in (3, 4)}
    assert len(list(tmp_path.glob(".*.transaction.json"))) == 2


def test_unknown_entries_and_unjournaled_candidates_are_preserved(tmp_path):
    orphan = release(tmp_path, 9)
    unrelated = tmp_path / "operator-notes"
    unrelated.mkdir()
    (tmp_path / ".not-a-release.transaction.json").write_text("not JSON")
    for number in range(1, 6):
        current = deploy(tmp_path, number, 0)
    assert orphan.candidate.exists()
    assert unrelated.exists()
    assert current.pointer() == current.digest


def test_staging_an_old_release_pins_it_until_activation(tmp_path):
    old = deploy(tmp_path, 1, 20)
    for number in range(2, 4):
        deploy(tmp_path, number, 20)
    # Re-stage the exact sealed bytes, as a historical operator rollback does.
    files = {"nginx.conf": (old.candidate / "nginx.conf").read_bytes()}
    bundle = tmp_path / ".bundle.json"
    bundle.write_text(
        json.dumps(
            {
                "files": {k: base64.b64encode(v).decode() for k, v in files.items()},
                "links": {},
                "manifest": old.inventory(),
            }
        )
    )
    old.stage(bundle)
    assert old.staged.exists()
    deploy(tmp_path, 4, 0)
    assert old.candidate.exists()
    old.activate()
    assert not old.staged.exists()
    old.commit(0)
    assert old.pointer() == old.digest
    assert trees(tmp_path) == {old.digest, f"{4:064x}"}


@pytest.mark.parametrize("entry", ["tree", "journal"])
def test_managed_symlinks_never_delete_external_data(tmp_path, capsys, entry):
    root = tmp_path / "releases"
    for number in range(1, 5):
        current = deploy(root, number, 20)
    external = tmp_path / "external"
    external.mkdir()
    (external / "precious").write_text("keep")
    old = ConfigurationRelease(root, f"{1:064x}", ())
    if entry == "tree":
        shutil.rmtree(old.candidate)
        old.candidate.symlink_to(external, target_is_directory=True)
    else:
        old.journal.unlink()
        old.journal.symlink_to(external / "precious")
    current.commit(0)
    assert (external / "precious").read_text() == "keep"
    assert "cleanup deferred" in capsys.readouterr().err


def test_cli_mutation_waits_for_edge_lock_and_passes_retention(tmp_path):
    for number in range(1, 5):
        current = deploy(tmp_path, number, 20)
    with (tmp_path / ".mutation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = subprocess.Popen(  # noqa: S603 - fixed local helper argv
            [
                sys.executable,
                str(_HELPER),
                "commit",
                str(tmp_path),
                current.digest,
                "--keep-older",
                "0",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.3)
            assert len(trees(tmp_path)) == 4
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            output, error = process.communicate(timeout=10)
    assert process.returncode == 0, (output, error)
    assert len(trees(tmp_path)) == 2


def test_negative_retention_is_rejected_before_commit(tmp_path):
    current = release(tmp_path, 1)
    current.activate()
    with pytest.raises(ValueError, match="negative"):
        current.commit(-1)
    assert current.read_journal()["status"] == "pending"


@pytest.mark.parametrize("check", [False, True])
def test_ansible_commit_applies_retention_only_outside_check_mode(tmp_path, check):
    import yaml
    from support.paths import REPO_ROOT

    root = tmp_path / "releases"
    for number in range(1, 5):
        current = deploy(root, number, 20)
    play = tmp_path / "commit.yml"
    play.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "ansible_python_interpreter": sys.executable,
                        "blitzecdn_release_digest": current.digest,
                        "blitzecdn_config_release_root": str(root),
                        "blitzecdn_config_release_helper": str(_HELPER),
                        "blitzecdn_config_release_keep_older": 0,
                    },
                    "tasks": [
                        {
                            "ansible.builtin.import_role": {
                                "name": "blitzecdn_config_release",
                                "tasks_from": "commit.yml",
                            }
                        }
                    ],
                }
            ]
        )
    )
    result = subprocess.run(  # noqa: S603 - fixed local Ansible argv
        [
            str(Path(sys.executable).with_name("ansible-playbook")),
            "-i",
            "localhost,",
            "-c",
            "local",
            str(play),
            *(["--check"] if check else []),
        ],
        env={
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("COV_CORE", "COVERAGE"))
        }
        | {
            "ANSIBLE_CONFIG": str(CORE_ANSIBLE / "ansible.cfg"),
            "ANSIBLE_ROLES_PATH": str(CORE_ANSIBLE / "roles"),
            "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-temp"),
            "ANSIBLE_COLLECTIONS_PATH": str(REPO_ROOT / ".state/collections"),
            "ANSIBLE_STDOUT_CALLBACK": "ansible.builtin.default",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(trees(root)) == (4 if check else 2)
    assert (root / ".mutation.lock").exists() is not check


def tls_release(root, number, *pairs):
    """A deployed release whose site configuration names managed TLS versions."""
    candidate = ConfigurationRelease(root, f"{number:064x}", ())
    candidate.candidate.mkdir(parents=True)
    (candidate.candidate / "nginx.conf").write_text("events {}\nhttp {}\n")
    sites = candidate.candidate / "nginx/sites-available"
    sites.mkdir(parents=True)
    for site, chain, key in pairs:
        (sites / f"{site}.conf").write_text(
            f"ssl_certificate {chain};\nssl_certificate_key {key};\n"
        )
    candidate.seal_candidate()
    return candidate


def material(tls, site, digit):
    directory = tls / site
    directory.mkdir(parents=True, exist_ok=True)
    chain = directory / f"fullchain-{digit * 64}.pem"
    key = directory / f"privkey-{digit * 64}.pem"
    chain.write_text("chain")
    key.write_text("key")
    return site, str(chain), str(key)


def tls_files(tls):
    return {str(p.relative_to(tls)) for p in tls.rglob("*")}


def test_commit_collects_tls_versions_no_retained_release_references(tmp_path):
    root, tls = tmp_path / "releases", tmp_path / "tls"
    for number in (1, 2, 3):
        current = tls_release(root, number, material(tls, "example-com", str(number)))
        current.activate()
        current.commit(0, tls)
    # Release 2 is the rollback target, so its version stays; 1 is gone.
    assert tls_files(tls) == {
        "example-com",
        f"example-com/fullchain-{'2' * 64}.pem",
        f"example-com/privkey-{'2' * 64}.pem",
        f"example-com/fullchain-{'3' * 64}.pem",
        f"example-com/privkey-{'3' * 64}.pem",
    }


def test_commit_removes_deleted_sites_and_ansible_backups_but_nothing_else(
    tmp_path,
):
    root, tls = tmp_path / "releases", tmp_path / "tls"
    kept = material(tls, "kept", "a")
    material(tls, "deleted", "b")
    backup = tls / "kept" / f"privkey-{'a' * 64}.pem.4242.2026-10-27@13:50:26~"
    backup.write_text("old key")
    (tls / "kept" / "operator.pem").write_text("not ours")
    (tls / "loose.pem").write_text("not ours")
    external = tmp_path / "external"
    external.mkdir()
    (external / f"privkey-{'c' * 64}.pem").write_text("precious")
    (tls / "linked").symlink_to(external)
    for number in (1, 2):
        current = tls_release(root, number, kept)
        current.activate()
        current.commit(0, tls)
    assert tls_files(tls) == {
        "kept",
        f"kept/fullchain-{'a' * 64}.pem",
        f"kept/privkey-{'a' * 64}.pem",
        "kept/operator.pem",
        "loose.pem",
        "linked",
    }
    assert (external / f"privkey-{'c' * 64}.pem").read_text() == "precious"


def test_unjournaled_candidate_pins_its_tls_material(tmp_path):
    root, tls = tmp_path / "releases", tmp_path / "tls"
    serving = material(tls, "example-com", "1")
    staged = material(tls, "example-com", "2")
    tls_release(root, 9, staged)
    current = tls_release(root, 1, serving)
    current.activate()
    current.commit(0, tls)
    assert {Path(p) for p in (*serving[1:], *staged[1:])} <= set(tls.rglob("*"))


def test_tls_collection_is_deferred_when_release_collection_fails(tmp_path, capsys):
    root, tls = tmp_path / "releases", tmp_path / "tls"
    orphan = material(tls, "deleted", "9")
    (root).mkdir()
    (root / f".{'f' * 64}.transaction.json").write_text("[]")
    current = tls_release(root, 1, material(tls, "example-com", "1"))
    current.activate()
    current.commit(0, tls)
    assert Path(orphan[2]).exists()
    assert "cleanup deferred" in capsys.readouterr().err


def test_cli_commit_passes_the_tls_root(tmp_path):
    root, tls = tmp_path / "releases", tmp_path / "tls"
    for number, digit in ((1, "1"), (2, "2")):
        current = tls_release(root, number, material(tls, "example-com", digit))
        current.activate()
        current.commit(20)
    current = tls_release(root, 3, material(tls, "example-com", "3"))
    current.activate()
    subprocess.run(  # noqa: S603 - fixed local helper argv
        [
            sys.executable,
            str(_HELPER),
            "commit",
            str(root),
            current.digest,
            "--keep-older",
            "0",
            "--tls-root",
            str(tls),
        ],
        check=True,
    )
    assert not (tls / "example-com" / f"privkey-{'1' * 64}.pem").exists()
    assert (tls / "example-com" / f"privkey-{'2' * 64}.pem").exists()
