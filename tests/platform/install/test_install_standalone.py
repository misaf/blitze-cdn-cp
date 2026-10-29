"""What a `standalone` install guarantees about the host it produces.

Structural assertions no unprivileged run can reach: the properties are read
off the script and the role rather than observed, and each says why it cannot
be exercised instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from support.install import (
    FAKE_IMAGE,
    fake_installation,
    instrument,
    query,
    role_task,
    role_tasks,
    run_sandboxed,
    script_function,
    script_section,
    script_text,
    stub_bin,
)

from blitzecdn.capabilities.releases.service import compiler


# --- structural guarantees no unprivileged run can reach ---------------------
def test_standalone_keeps_the_management_api_on_loopback():
    script = script_text()
    assert "ssh -L 8000:127.0.0.1:8000" in script
    assert "--host 0.0.0.0" not in script


def test_standalone_defaults_to_no_deployment():
    """Preparing a server and deploying to it are separate decisions."""
    required = "--admin-cidr 203.0.113.8/32 --email operator@example.com"
    assert (
        query(
            f"parse_options standalone usage_standalone {required}\n"
            'echo "${parsed_deploy}"'
        )
        == "0"
    )
    assert (
        query(
            f"parse_options standalone usage_standalone {required} --deploy\n"
            'echo "${parsed_deploy}"'
        )
        == "1"
    )
    # Forwarding happens inside a root-only command, so it stays structural.
    assert "handoff_args+=(--deploy)" in script_section("standalone")


@pytest.mark.parametrize(
    ("distribution", "version", "accepted"),
    [
        ("ubuntu", "26.04", True),
        # Supported control-plane platforms that cannot be edges, which is why this
        # check is separate from the role's.
        ("ubuntu", "24.04", False),
        ("debian", "13", False),
        ("ubuntu", "26.10", False),
        ("", "", False),
    ],
)
def test_a_standalone_host_must_satisfy_the_edge_platform(
    distribution: str, version: str, accepted: bool
):
    """The narrower of the two contracts a standalone server signs.

    The control-plane role takes Debian 13+ and Ubuntu 24.04+; the edge play only
    Ubuntu 26.04. A standalone host is both, so it must meet the edge's.
    """
    supported = (
        query(
            f'edge_platform_supported "{distribution}" "{version}" '
            "&& echo yes || echo no"
        )
        == "yes"
    )
    assert supported is accepted


def test_the_platform_is_checked_before_anything_is_installed():
    """Before apt, as --admin-cidr is."""
    standalone = script_section("standalone")
    assert standalone.index("require_edge_platform") < standalone.index("apt-get")
    # Not the release-move command: it converges the control plane alone, and
    # refusing it would strand an existing host.
    assert "require_edge_platform" not in script_section("update")


def test_the_inputs_are_checked_by_the_image_before_the_host_converges():
    """The host has no copy of the validators; the image about to act does."""
    standalone = script_section("standalone")
    checked = standalone.index('check_inputs "${image}" standalone')
    assert standalone.index("obtain_control_plane_image") < checked
    assert checked < standalone.index("converge_control_plane")
    assert standalone.index('allowed_ips_value "${image}"') < standalone.index(
        "converge_control_plane"
    )


def _standalone_sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    """An installed host on 3.0.0 whose apt records instead of installing."""
    sandbox = tmp_path / "sandbox"
    script, root = instrument(sandbox)
    stub_bin(sandbox, root)
    fake_installation(root)
    apt = sandbox / "bin" / "apt-get"
    apt.write_text(
        f'#!/usr/bin/env bash\necho "$*" >> "{tmp_path / "apt.log"}"\nexit 1\n',
        encoding="utf-8",
    )
    apt.chmod(0o700)
    return script, root, tmp_path / "apt.log"


def test_standalone_refuses_a_host_that_runs_another_release(tmp_path: Path):
    """An old download re-run on a newer host would be an irreversible downgrade."""
    script, _, apt = _standalone_sandbox(tmp_path)

    result = run_sandboxed(
        script, "standalone", "--admin-cidr", "203.0.113.8/32", "--email", "a@b.example"
    )

    assert result.returncode == 1
    assert "this host runs 3.0.0" in result.stderr
    assert "nothing was changed" in result.stderr
    assert "update" in result.stderr
    assert not apt.exists(), "nothing may be installed"


def test_standalone_repairs_the_release_that_is_installed(tmp_path: Path):
    """The same release goes on to apt, past the release check."""
    script, _, apt = _standalone_sandbox(tmp_path)

    result = run_sandboxed(
        script,
        "standalone",
        "--admin-cidr",
        "203.0.113.8/32",
        "--email",
        "a@b.example",
        env_extra={"BLITZECDN_CONTROL_PLANE_IMAGE": FAKE_IMAGE},
    )

    assert result.returncode == 1, "stopped by the apt stub"
    assert apt.read_text(encoding="utf-8").splitlines()[0] == "update"


def test_standalone_collects_the_access_list_from_either_spelling():
    """Repeat the flag or hand it a list; the role receives one value."""
    required = "--admin-cidr 203.0.113.8/32 --email operator@example.com"
    assert (
        query(
            f"parse_options standalone usage_standalone {required} "
            "--allowed-ips 203.0.113.8/32 --allowed-ips 198.51.100.0/24\n"
            'printf "%s\\n" "${parsed_allowed_ips[@]}"'
        )
        == "203.0.113.8/32\n198.51.100.0/24"
    )
    assert (
        query(
            f"parse_options standalone usage_standalone {required}\n"
            'echo "${#parsed_allowed_ips[@]}"'
        )
        == "0"
    )
    assert (
        query(
            f"parse_options standalone usage_standalone {required} "
            "--allowed-ips '203.0.113.8/32, 198.51.100.0/24'\n"
            "run_image_helper() { return 0; }\n"
            "allowed_ips_value image"
        )
        == "203.0.113.8/32, 198.51.100.0/24"
    )
    # The extra-var lives inside a root-only command, so the last hop stays
    # structural.
    assert (
        '--extra-vars "blitzecdn_controlplane_allowed_ips=${allowed_ips}"'
        in script_section("standalone")
    )


def test_a_release_move_can_repoint_the_access_list():
    """Both commands that converge the control plane take the list.

    Omitting the flag, the common case, leaves the installed list untouched.
    """
    section = script_function("cmd_update")
    assert '--extra-vars "blitzecdn_controlplane_allowed_ips=${allowed_ips}"' in section
    # Before the point of no return, not after it. A rejected access list must
    # cost a refused command, never a host sitting with its services stopped.
    assert section.index('allowed_ips_value "${installed}"') < section.index("backup")
    assert section.index("allowed_ips_value") < section.index("ensure_docker")


def test_the_access_list_survives_an_empty_flag_on_bash_3():
    """macOS ships Bash 3.2, which calls an empty array unbound under `set -u`.

    Every `update` evaluates this, flag or not.
    """
    assert (
        query(
            "parse_options update usage_update --yes\n"
            'allowed_ips_value image\necho "[empty]"'
        )
        == "[empty]"
    )


def test_standalone_guards_existing_sites_from_empty_desired_state():
    """The installer's flag and the variable it ends up as, in one assertion.

    The module is located by import rather than by path, so a move cannot
    silently break the check.
    """
    standalone = script_section("standalone")
    assert 'BLITZE_ALLOW_EMPTY_SITES="${parsed_allow_empty_sites}"' in standalone
    assert compiler.__file__ is not None
    assert "blitzecdn_nginx_allow_empty_sites" in Path(compiler.__file__).read_text(
        encoding="utf-8"
    )


def test_the_role_installs_the_installer_of_the_release_it_runs():
    """Copied out of the image, so an update replaces it with the target's own."""
    installer = role_task("Install the blitzecdn-install command")
    arguments = installer["ansible.builtin.copy"]
    assert arguments["src"] == "{{ blitzecdn_controlplane_install_dir }}/install.sh"
    assert arguments["dest"] == "{{ blitzecdn_controlplane_installer }}"
    assert (arguments["owner"], arguments["group"], arguments["mode"]) == (
        "root",
        "root",
        "0755",
    )


def test_a_server_has_no_source_tree():
    """The role makes no host directory for the project: only the image has one."""
    assert not any(
        "blitzecdn_controlplane_install_dir"
        in str(task.get("ansible.builtin.file", ""))
        for task in role_tasks()
    )


@pytest.mark.parametrize("body", ["standalone", "cmd_update"])
def test_a_server_installs_no_python_packages_and_asks_no_package_index(body: str):
    """The point of the image: a host that cannot reach PyPI can still install.

    Ansible, its collections and the application all come from the image, so
    a server never builds a virtualenv, never runs uv, and never asks Galaxy.
    """
    code = script_section(body) if body == "standalone" else script_function(body)
    for host_tooling in ("bootstrap_runtime", "uv", "ansible-galaxy", "pip"):
        assert not re.search(rf"\b{re.escape(host_tooling)}\b", code), host_tooling
    assert "ensure_docker" in code
    assert "obtain_control_plane_image" in code


def test_only_a_source_build_needs_the_package_index():
    """The fallback is documented, opt-in, and the only path that builds."""
    image = script_function("obtain_control_plane_image")
    assert "BLITZECDN_CONTROL_PLANE_BUILD" in image
    assert image.count("docker build") == 1
    assert image.index("BLITZECDN_CONTROL_PLANE_BUILD") < image.index("docker build")
    for help_form in ("standalone", "update"):
        assert "BLITZECDN_CONTROL_PLANE_BUILD=1" in query(f"usage_{help_form}")
