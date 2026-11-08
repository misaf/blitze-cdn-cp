"""The `blitzecdn_controlplane` role, which owns the host state.

Properties are asserted against the role's tasks under
``src/blitzecdn/assets/ansible/roles/``.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from support.install import (
    ROLE,
    fake_installation,
    instrument,
    role_task,
    role_tasks,
    run_sandboxed,
    script_function,
    script_section,
    stub_bin,
)
from support.paths import CORE_ANSIBLE

from blitzecdn.assets.docker import CONTROL_PLANE_DOCKERFILE

# --- the control-plane role --------------------------------------------------
#
# What the role does is covered by running it on a real host; these pin the
# invariants one successful run cannot show.


def test_host_key_scan_is_stable_across_runs():
    """`ssh-keyscan -H` salts each run, so every converge would report a change."""
    task = role_task("Scan the loopback host keys")
    assert "-H" not in task["ansible.builtin.command"]["argv"]
    written = role_task("Record the loopback host keys")
    assert "sort" in written["ansible.builtin.copy"]["content"]


def test_a_converge_never_rewrites_the_edge_host_keys():
    """Edge keys are enrolled by `edge add`; an update must not wipe them (#48)."""
    loopback = role_task("Record the loopback host keys")["ansible.builtin.copy"]
    assert loopback["dest"].endswith("/.ssh/known_hosts.loopback")
    edges = role_task("Keep the edge host keys file")["ansible.builtin.file"]
    assert edges["path"].endswith("/.ssh/known_hosts")
    assert edges["state"] == "touch"
    assert edges["modification_time"] == "preserve"
    writers = [
        task
        for task in role_tasks()
        if any(
            str(module.get("dest", "")).endswith("/.ssh/known_hosts")
            for module in task.values()
            if isinstance(module, dict)
        )
    ]
    assert writers == []


def test_controlplane_role_does_not_clean_up_host_application_units():
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
    assert "blitzecdn_controlplane_obsolete_units" not in defaults
    tasks = (ROLE / "tasks/main.yml").read_text(encoding="utf-8")
    assert "obsolete host application units" not in tasks


def test_controlplane_initializes_schema_before_starting_services():
    tasks = role_tasks()
    schema = role_task("Initialize the application schema before starting services")
    services = role_task("Recreate and start the control-plane services")

    stopped = role_task("Stop application services before schema migration")
    assert tasks.index(stopped) < tasks.index(schema) < tasks.index(services)
    assert stopped["ansible.builtin.command"]["argv"][-3:] == [
        "stop",
        "blitzecdn-api",
        "blitzecdn-worker",
    ]
    command = schema["ansible.builtin.command"]["argv"]
    assert command[-3:] == ["--no-deps", "blitzecdn-cli", "migrate"]


def test_controlplane_fails_the_play_unless_both_services_become_healthy():
    """`state: started` alone would leave a play green with a service down.

    This wait is the whole of "the update worked".
    """
    tasks = role_tasks()
    services = role_task("Recreate and start the control-plane services")
    wait = role_task("Wait for the API and worker containers to become healthy")

    assert tasks.index(services) < tasks.index(wait)
    assert wait["loop"] == ["blitzecdn-api", "blitzecdn-worker"]
    assert wait["until"] == "blitzecdn_controlplane_health.stdout == 'healthy'"
    assert wait["retries"] * wait["delay"] >= 60


def test_standalone_bootstraps_only_what_ansible_needs():
    """Everything else is the role's job; this list is the bootstrap contract."""
    standalone = script_section("standalone")
    # python3 runs the play's modules under nsenter; curl and ca-certificates
    # fetch Docker's signing key. No git: a server has no source tree.
    assert "apt-get install -y ca-certificates curl python3" in standalone
    assert standalone.index("ensure_docker") < standalone.index(
        "obtain_control_plane_image"
    )
    assert standalone.index("obtain_control_plane_image") < standalone.index(
        "converge_control_plane"
    )
    assert "converge_control_plane" in standalone
    # Accounts, sudo, SSH trust and units must not be done twice.
    for moved in ("useradd", "visudo", "ssh-keygen", "ssh-keyscan", "openssl rand"):
        assert moved not in standalone, f"{moved} still runs in the installer"


def test_local_lifecycle_playbooks_do_not_load_fleet_inventory():
    """Bootstrap precedes the database, and teardown must not depend on it.

    Both run through one function, so the inventory is stated once.
    """
    assert "-i localhost," in script_function("run_playbook")
    for helper in ("converge_control_plane", "converge_uninstall"):
        assert "run_playbook" in script_function(helper), (
            f"{helper} no longer runs through the shared playbook helper"
        )


def test_every_installer_playbook_can_resolve_the_collections_it_needs():
    """The teardown plays need third-party collections as much as the setup ones.

    `ansible.cfg` names no collections path. Every play runs in the image, whose
    environment names the directory its build installed `requirements.yml`
    into, so the image is the whole of it. Without it teardown stops at the
    first `community.docker` task, leaving the edge running.
    """
    body = script_function("run_playbook")
    assert '--entrypoint ansible-playbook "${image}"' in body
    assert "ANSIBLE_COLLECTIONS_PATH" not in body, (
        "the image already names its collections; a second answer here can only "
        "disagree with it"
    )
    dockerfile = CONTROL_PLANE_DOCKERFILE.read_text(encoding="utf-8")
    assert "ANSIBLE_COLLECTIONS_PATH=/usr/share/ansible/collections" in dockerfile
    assert "--collections-path /usr/share/ansible/collections" in dockerfile
    assert "src/blitzecdn/assets/ansible/requirements.yml" in dockerfile

    # Teardown needs a collection ansible-core does not carry, read from the roles.
    teardown = ""
    for role in ("blitzecdn_uninstall", "blitzecdn_edge_teardown"):
        for tasks in (CORE_ANSIBLE / f"roles/{role}").rglob("*.yml"):
            teardown += tasks.read_text(encoding="utf-8")
    assert "community.docker." in teardown, (
        "the uninstall roles no longer use a third-party collection; if that is "
        "deliberate, this guard has outlived the failure it describes"
    )


def test_uninstall_succeeds_after_ansible_teardown(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    script, root = instrument(sandbox)
    stub_bin(sandbox, root)
    fake_installation(root)

    result = run_sandboxed(script, "--uninstall", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "BlitzeCDN has been removed" in result.stdout


def test_edge_platform_is_pinned_to_ubuntu_26_04():
    edge = yaml.safe_load(
        (CORE_ANSIBLE / "playbooks/edge.yml").read_text(encoding="utf-8")
    )
    edge_gate = edge[0]["pre_tasks"][0]["ansible.builtin.assert"]
    assert edge_gate["that"] == [
        "ansible_facts.distribution == 'Ubuntu'",
        "ansible_facts.distribution_version == '26.04'",
    ]
    assert "Ubuntu 26.04 LTS" in edge_gate["fail_msg"]


# --- recovering from a failed run (#175) ---------------------------------------


def _converge_block() -> dict:
    return role_task("Converge the release this run installs")


def test_the_installed_release_is_kept_before_anything_replaces_it():
    """Each file the release runs from is copied aside before the first writer."""
    tasks = role_tasks()
    kept = role_task("Copy the installed release's files aside")
    named = role_task("Name the files the installed release runs from")
    files = named["ansible.builtin.set_fact"]["blitzecdn_controlplane_release_files"]
    for path in (
        "{{ blitzecdn_controlplane_compose_file }}",
        "{{ blitzecdn_controlplane_config_dir }}/compose.env",
        "{{ blitzecdn_controlplane_config_dir }}/blitzecdn.toml",
        "{{ blitzecdn_controlplane_cli_wrapper }}",
        "{{ blitzecdn_controlplane_installer }}",
    ):
        assert path in files
    first_writer = role_task("Install the blitzecdn command")
    assert tasks.index(kept) < tasks.index(first_writer)
    assert kept["ansible.builtin.copy"]["remote_src"] is True
    assert kept["ansible.builtin.copy"]["mode"] == "preserve"
    # An older run's copies never stand in for this installation's.
    cleared = role_task("Clear the previous release's kept files")
    assert tasks.index(cleared) < tasks.index(kept)


def test_every_step_that_replaces_the_release_is_inside_the_rescued_block():
    block = _converge_block()["block"]
    names = [task["name"] for task in block]
    for step in (
        "Install the blitzecdn command",
        "Install the blitzecdn-install command",
        "Write the non-secret service configuration",
        "Render the control-plane Compose project",
        "Read the schema revision before the migration",
        "Stop application services before schema migration",
        "Initialize the application schema before starting services",
        "Recreate and start the control-plane services",
        "Wait for the API and worker containers to become healthy",
    ):
        assert step in names, f"{step!r} is outside the rescued block"
    assert names.index("Read the schema revision before the migration") < names.index(
        "Record that this run stops the services"
    )
    assert names.index("Record that this run stops the services") < names.index(
        "Stop application services before schema migration"
    )


def test_the_schema_revision_is_read_by_the_image_helper_on_both_sides():
    for name in (
        "Read the schema revision before the migration",
        "Read the schema revision the failed run left",
    ):
        argv = role_task(name)["ansible.builtin.command"]["argv"]
        assert argv[-6:] == [
            "--entrypoint",
            "python",
            "blitzecdn-cli",
            "-I",
            "-m",
            "blitzecdn.host.schema_revision",
        ]
        assert "--no-deps" in argv


def test_a_failed_run_restarts_the_previous_release_only_on_an_unchanged_schema():
    rescue = _converge_block()["rescue"]
    names = [task["name"] for task in rescue]
    restore = names.index("Restore the previous release's files")
    start = names.index("Start the previous release again")
    assert names.index("Decide what the failed run changed") < restore < start
    started = rescue[start]
    assert "not blitzecdn_controlplane_recovery_migrated | bool" in started["when"]
    argv = started["ansible.builtin.command"]["argv"]
    assert "up" in argv and "--wait" in argv
    # Recreated only where the restored project differs: services this run never
    # stopped keep running.
    assert "--force-recreate" not in argv
    # The run still fails: an operator must learn the update did not happen.
    assert names[-1] == "Stop with what happened and how to recover"
    assert "ansible.builtin.fail" in rescue[-1]


def test_an_unreadable_revision_counts_as_migrated():
    """Starting old code on a schema it does not know is the one wrong answer."""
    decision = role_task("Decide what the failed run changed")
    migrated = decision["ansible.builtin.set_fact"][
        "blitzecdn_controlplane_recovery_migrated"
    ]
    assert "blitzecdn_controlplane_schema_before.rc | default(1) == 0" in migrated
    assert "blitzecdn_controlplane_schema_after.rc | default(1) == 0" in migrated


def test_a_migrated_failure_names_the_backup_and_the_restore_commands():
    message = role_task("Stop with what happened and how to recover")[
        "ansible.builtin.fail"
    ]["msg"]
    assert "blitzecdn_controlplane_recovery_backup" in message
    named = role_task("Name the previous release's image")["ansible.builtin.set_fact"]
    assert (
        "blitzecdn_controlplane_update_backup"
        in (named["blitzecdn_controlplane_recovery_backup"])
    )
    assert "blitzecdn backup restore ARCHIVE --yes" in message
    assert "up --detach --wait" in message


def test_the_kept_files_are_root_only_and_go_with_the_uninstall():
    """compose.env among them holds the credentials."""
    created = role_task("Create the directory for the installed release's files")
    assert created["ansible.builtin.file"]["mode"] == "0700"
    assert created["ansible.builtin.file"]["owner"] == "root"
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
    assert defaults["blitzecdn_controlplane_previous_release_dir"].startswith(
        defaults["blitzecdn_controlplane_config_dir"] + "/"
    )
    assert defaults["blitzecdn_controlplane_update_backup"] == ""
