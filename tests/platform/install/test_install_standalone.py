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
    query,
    role_task,
    script_function,
    script_section,
    script_text,
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
    # Not the release-move commands: they converge the control plane alone, and
    # refusing them would strand an existing host.
    assert "require_edge_platform" not in script_section("update")


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
            "allowed_ips_value"
        )
        == "203.0.113.8/32, 198.51.100.0/24"
    )
    # The extra-var lives inside a root-only command, so the last hop stays
    # structural.
    assert (
        '--extra-vars "blitzecdn_controlplane_allowed_ips=${allowed_ips}"'
        in script_section("standalone")
    )


@pytest.mark.parametrize("subcommand", ["update", "upgrade"])
def test_a_release_move_can_repoint_the_access_list(subcommand: str):
    """The three commands that converge the control plane all take the list.

    Omitting the flag, the common case, leaves the installed list untouched.
    """
    assert f"move_release {subcommand}" in script_section("update")
    section = script_function("move_release")
    assert '--extra-vars "blitzecdn_controlplane_allowed_ips=${allowed_ips}"' in section
    # Before the point of no return, not after it. A rejected access list must
    # cost a refused command, never a host sitting with its services stopped.
    assert section.index("allowed_ips_value") < section.index(
        'repo_git checkout --quiet "${target}"'
    )
    assert section.index("allowed_ips_value") < section.index("backup")


def test_the_access_list_survives_an_empty_flag_on_bash_3():
    """macOS ships Bash 3.2, which calls an empty array unbound under `set -u`.

    Every `update` evaluates this, flag or not.
    """
    assert (
        query(
            'parse_options update usage_update --yes\nallowed_ips_value\necho "[empty]"'
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


def test_role_keeps_the_installation_tree_root_owned_and_read_only_to_runtime():
    installation = role_task("Set the installation directory mode")
    arguments = installation["ansible.builtin.file"]
    assert arguments["owner"] == "root"
    assert arguments["group"] == "root"
    assert arguments["mode"] == "0755"
    assert arguments.get("recurse") is not True


def test_installer_installs_only_third_party_collections():
    """The BlitzeCDN roles ship inside the package; nothing pins or builds them."""
    bootstrap = script_function("bootstrap_runtime")
    assert (
        '-r src/blitzecdn/assets/ansible/requirements.yml -p "${collections_path}"'
        in bootstrap
    )
    assert "--force" not in bootstrap
    assert "collection build" not in script_text()


@pytest.mark.parametrize("body", ["standalone", "move_release"])
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


def test_installer_preserves_rather_than_deletes_an_incomplete_virtualenv():
    script = script_text()
    # The interpreter is the tell, not `pip`: uv builds the environment without
    # pip.
    assert "! -x .venv/bin/python" in script
    assert ".venv.invalid.$(date -u +%Y%m%dT%H%M%SZ)" in script
