"""The `blitzecdn_controlplane` role, which owns the host state.

Properties are asserted against the role's tasks under
``src/blitzecdn/assets/ansible/roles/``.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from support.install import (
    PROJECT_DIR,
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


def test_controlplane_role_has_no_legacy_host_application_unit_cleanup():
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
    # python3 runs the input checks and, under nsenter, the play's modules;
    # curl and ca-certificates fetch Docker's signing key.
    assert "apt-get install -y ca-certificates curl git python3" in standalone
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


def test_no_upper_bound_on_the_python_version():
    """A ceiling is discovered on somebody's fresh server, never in CI."""
    script = (PROJECT_DIR / "install.sh").read_text(encoding="utf-8")
    assert "sys.version_info[:2] < (3, 12)" in script
    assert "(3, 15)" not in script, "install.sh still refuses a future Python"
