"""Exercise the exact stdlib helper shipped to an edge, on a real filesystem."""

import os
import runpy
from pathlib import Path

import pytest
from paths import CORE_ANSIBLE

_HELPER = CORE_ANSIBLE / "roles/blitzecdn_config_release/files/configuration_release.py"
ConfigurationRelease = runpy.run_path(str(_HELPER))["ConfigurationRelease"]


def candidate(root: Path, digest: str, records=()):
    tree = root / digest
    tree.mkdir(parents=True)
    (tree / "nginx.conf").write_text("include this-release.conf;")
    (tree / "this-release.conf").write_text(digest)
    release = ConfigurationRelease(root, digest, tuple(records))
    release.seal_candidate()
    return release


def test_activation_and_rollback_restore_the_whole_previous_definition(tmp_path):
    compose = tmp_path / "compose.yml"
    image = tmp_path / "image.json"
    compose.write_text("old compose")
    image.write_text("old image")
    root = tmp_path / "releases"
    previous = candidate(root, "a" * 64, (compose, image))
    previous.activate()
    previous.commit()
    following = candidate(root, "b" * 64, (compose, image))
    following.activate()
    compose.write_text("new compose")
    image.write_text("new image")

    assert os.readlink(root / "current") == following.digest
    # Reconstruct exactly as a restarted controller's new helper process does.
    resumed = ConfigurationRelease(root, following.digest, ())
    resumed.activate()
    resumed.rollback()
    assert os.readlink(root / "current") == previous.digest
    assert compose.read_text() == "old compose"
    assert image.read_text() == "old image"
    resumed.rollback()
    assert compose.read_text() == "old compose"


def test_first_installation_failure_withdraws_new_records_and_pointer(tmp_path):
    compose = tmp_path / "compose.yml"
    release = candidate(tmp_path / "releases", "a" * 64, (compose,))
    release.activate()
    compose.write_text("failed first compose")
    release.rollback()
    assert not os.path.lexists(release.current)
    assert not compose.exists()
    release.rollback()


def test_crash_after_journal_before_pointer_switch_is_replayable(tmp_path, monkeypatch):
    release = candidate(tmp_path / "releases", "a" * 64)
    switch = ConfigurationRelease.switch

    def crash(self, digest):
        raise OSError("power lost")

    monkeypatch.setattr(ConfigurationRelease, "switch", crash)
    with pytest.raises(OSError, match="power lost"):
        release.activate()
    assert release.read_journal()["previous"] is None
    assert not release.current.exists()
    monkeypatch.setattr(ConfigurationRelease, "switch", switch)
    release.activate()
    release.rollback()
    assert not os.path.lexists(release.current)


def test_tampered_sealed_tree_is_refused_before_activation(tmp_path):
    root = tmp_path / "releases"
    previous = candidate(root, "a" * 64)
    previous.activate()
    previous.commit()
    release = candidate(root, "b" * 64)
    (release.candidate / "this-release.conf").write_text("changed after validation")
    with pytest.raises(ValueError, match="content changed"):
        release.activate()
    assert os.readlink(release.current) == previous.digest
    assert not release.journal.exists()


def test_seal_includes_njs_and_checks_link_targets(tmp_path):
    release = candidate(tmp_path / "releases", "a" * 64)
    release.seal.unlink()
    njs = release.candidate / "challenge.js"
    njs.write_text("original secret")
    link = release.candidate / "enabled.conf"
    link.symlink_to(release.candidate / "this-release.conf")
    release.seal_candidate()
    release.verify()
    njs.write_text("replacement secret")
    with pytest.raises(ValueError, match="content changed"):
        release.verify()
    link.unlink()
    link.symlink_to("/etc/passwd")
    with pytest.raises(ValueError):
        release.inventory()


def test_committed_activation_cannot_be_rolled_back_by_a_late_request(tmp_path):
    release = candidate(tmp_path / "releases", "a" * 64)
    release.activate()
    release.commit()
    release.commit()
    release.activate()
    with pytest.raises(ValueError, match="committed"):
        release.rollback()
    assert release.pointer() == release.digest


def test_rollback_cannot_overwrite_a_newer_activation(tmp_path):
    root = tmp_path / "releases"
    first = candidate(root, "a" * 64)
    second = candidate(root, "b" * 64)
    first.activate()
    second.activate()
    with pytest.raises(ValueError, match="superseded"):
        first.rollback()
    assert first.pointer() == second.digest


def test_withdrawn_candidate_can_be_activated_again_with_a_fresh_journal(tmp_path):
    root = tmp_path / "releases"
    first = candidate(root, "a" * 64)
    first.activate()
    first.rollback()
    second = candidate(root, "b" * 64)
    second.activate()
    second.commit()
    first.activate()
    first.rollback()
    assert first.pointer() == second.digest


@pytest.mark.parametrize("digest", ["../other", "a" * 63, "A" * 64, ""])
def test_release_paths_cannot_escape_the_managed_directory(tmp_path, digest):
    with pytest.raises(ValueError, match="digest"):
        ConfigurationRelease(tmp_path, digest, ())


@pytest.mark.parametrize("phase", ["stage", "rollback"])
def test_real_ansible_selects_candidate_paths_and_matching_parent_mounts(
    tmp_path, phase
):
    """Evaluate set_fact and lazy role defaults, rather than imitate Jinja here."""
    import json
    import subprocess
    import sys

    import yaml
    from role_contract_support import _render_compose

    root = str(tmp_path / "releases")
    digest = "a" * 64
    if phase == "rollback":
        # Recovery must not depend on reading the broken candidate's image or
        # manifest: only the retained previous tree must validate on rollback.
        tree = Path(root) / digest
        tree.mkdir(parents=True)
        (tree / "manifest.json").write_text("corrupted")
    play = tmp_path / "select.yml"
    play.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "blitzecdn_release_digest": digest,
                        "blitzecdn_config_release_root": root,
                        "blitzecdn_config_release_helper": str(tmp_path / "helper.py"),
                    },
                    "tasks": [
                        {"ansible.builtin.import_role": {"name": "blitzecdn_edge"}},
                        {
                            "ansible.builtin.import_role": {
                                "name": "blitzecdn_config_release",
                                "tasks_from": "select.yml",
                            }
                        },
                        {
                            "ansible.builtin.assert": {
                                "that": [
                                    "blitzecdn_edge_runtime.paths.nginx == "
                                    'blitzecdn_config_release_candidate ~ "/nginx"',
                                    "blitzecdn_edge_runtime.paths.modules == "
                                    'blitzecdn_config_release_candidate ~ "/njs"',
                                    "blitzecdn_nginx_config_test_volumes[0] == "
                                    'blitzecdn_config_release_root ~ ":" ~ '
                                    'blitzecdn_config_release_root ~ ":ro"',
                                ]
                            }
                        },
                        # debug emits a machine-readable value under the JSON callback.
                        {
                            "ansible.builtin.debug": {
                                "var": "blitzecdn_nginx_config_test_volumes"
                            }
                        },
                    ],
                }
            ]
        )
    )
    document = yaml.safe_load(play.read_text())
    for task in document[0]["tasks"]:
        task["tags"] = ["always"]
    play.write_text(yaml.safe_dump(document))
    result = subprocess.run(  # noqa: S603 - fixed local playbook argv
        [
            str(Path(sys.executable).with_name("ansible-playbook")),
            "-i",
            "localhost,",
            "-c",
            "local",
            "--check",
            "--tags",
            phase,
            str(play),
        ],
        env={
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("COV_CORE", "COVERAGE"))
        }
        | {
            "ANSIBLE_ROLES_PATH": str(CORE_ANSIBLE / "roles"),
            "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-temp"),
            "ANSIBLE_STDOUT_CALLBACK": "ansible.builtin.default",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Read the debug list from the default callback's JSON-shaped output.
    marker = '"blitzecdn_nginx_config_test_volumes": '
    volumes, _ = json.JSONDecoder().raw_decode(result.stdout.split(marker)[-1])
    compose = _render_compose(
        blitzecdn_config_release_enabled=True, blitzecdn_config_release_root=root
    )
    assert volumes == compose["services"]["edge"]["volumes"]
    assert compose["services"]["edge"]["command"][-1] == root + "/current/nginx.conf"
    assert not (tmp_path / "helper.py").exists(), "check mode must not install anything"
