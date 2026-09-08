"""The `blitzecdn_controlplane` role, which owns the host state.

Host state lives in the role under ``src/blitzecdn/ansible/roles/`` rather than
in bash, so the properties are asserted against the role's tasks.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from install_support import (
    PROJECT_DIR,
    ROLE,
    _fake_installation,
    _function,
    _instrument,
    _role_task,
    _role_tasks,
    _run_sandboxed,
    _section,
    _stub_bin,
)
from paths import CORE_ANSIBLE

# --- the control-plane role --------------------------------------------------
#
# Host state moved out of this script and into the blitzecdn_controlplane
# role under src/blitzecdn/ansible/roles/, so the properties that used to be
# asserted against bash are asserted against the role's tasks. What the role
# *does* is covered by running it on a real Debian/Ubuntu host; these pin the
# invariants that a reader cannot see from one successful run.


def test_host_key_scan_is_stable_across_runs():
    """`ssh-keyscan -H` salts each run, so every converge would report a change."""
    task = _role_task("Scan the loopback host keys")
    assert "-H" not in task["ansible.builtin.command"]["argv"]
    written = _role_task("Record the loopback host keys")
    assert "sort" in written["ansible.builtin.copy"]["content"]


def test_controlplane_role_has_no_legacy_host_application_unit_cleanup():
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
    assert "blitzecdn_controlplane_obsolete_units" not in defaults
    tasks = (ROLE / "tasks/main.yml").read_text(encoding="utf-8")
    assert "obsolete host application units" not in tasks


def test_controlplane_initializes_schema_before_starting_services():
    tasks = _role_tasks()
    schema = _role_task("Initialize the application schema before starting services")
    services = _role_task("Recreate and start the control-plane services")

    assert tasks.index(schema) < tasks.index(services)
    command = schema["ansible.builtin.command"]["argv"]
    assert command[-4:] == ["--no-deps", "blitzecdn-cli", "setup", "--schema-only"]


def test_standalone_bootstraps_only_what_ansible_needs():
    """Everything else is the role's job; this list is the bootstrap contract."""
    standalone = _section("standalone")
    # uv creates the virtualenv itself, so python3-venv left the list; curl and
    # ca-certificates joined it because the pinned uv is fetched over HTTPS.
    assert "apt-get install -y ca-certificates curl git python3" in standalone
    assert "converge_control_plane" in standalone
    # Accounts, sudo, SSH trust and units must not be done twice.
    for moved in ("useradd", "visudo", "ssh-keygen", "ssh-keyscan", "openssl rand"):
        assert moved not in standalone, f"{moved} still runs in the installer"


def test_local_lifecycle_playbooks_do_not_load_fleet_inventory():
    """Bootstrap precedes the database, and teardown must not depend on it.

    Both convergences run through one function now, so the inventory is stated
    once — which is also what stops the two from drifting apart.
    """
    assert "-i localhost," in _function("run_playbook")
    for helper in ("converge_control_plane", "converge_uninstall"):
        assert "run_playbook" in _function(helper), (
            f"{helper} no longer runs through the shared playbook helper"
        )


def test_every_installer_playbook_can_resolve_the_collections_it_needs():
    """The teardown plays need third-party collections as much as the setup ones.

    `ansible.cfg` deliberately does not name a collections path — a relative
    one there resolves inside the wheel the roles ship in — so an exported
    ANSIBLE_COLLECTIONS_PATH is the whole of it. It used to be exported only by
    `bootstrap_runtime`, which builds the virtualenv and fetches the
    collections, and which `--uninstall` has no reason to call.

    So the teardown ran with no collections and stopped at the first
    `community.docker` task. That task is the one that removes the edge's
    containers, which left the host running an edge with an installer that had
    just told the operator it could not continue.

    Absolute, too: relative to the working directory it is only correct when
    the caller happens to be standing in the installation.
    """
    body = _function("run_playbook")
    assert 'ANSIBLE_COLLECTIONS_PATH="${INSTALL_DIR}/.state/collections"' in body, (
        "the shared playbook helper does not point Ansible at the collections, "
        "so any play the bootstrap did not precede resolves none of them"
    )

    # The requirement is real rather than defensive: teardown reaches for a
    # collection ansible-core does not carry, and reads it from the roles
    # rather than restating the name.
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
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    _fake_installation(root)

    result = _run_sandboxed(script, "--uninstall", "--yes")

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
    """A ceiling is discovered on somebody's fresh server, never in CI.

    An interpreter that genuinely breaks the control plane should fail the test
    suite, not be refused at install time on a host nobody has tried yet.
    """
    script = (PROJECT_DIR / "install.sh").read_text(encoding="utf-8")
    assert "sys.version_info[:2] < (3, 12)" in script
    assert "(3, 15)" not in script, "install.sh still refuses a future Python"
