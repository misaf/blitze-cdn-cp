"""What a `standalone` install guarantees about the host it produces.

Structural assertions no unprivileged run can reach: the properties are read
off the script and the role rather than observed, and each says why it cannot
be exercised instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from install_support import (
    _query,
    _role_task,
    _script,
    _section,
)

from blitzecdn.capabilities.deployments.adapters import desired_state


# --- structural guarantees no unprivileged run can reach ---------------------
def test_standalone_keeps_the_management_api_on_loopback():
    script = _script()
    assert "ssh -L 8000:127.0.0.1:8000" in script
    assert "--host 0.0.0.0" not in script


def test_standalone_defaults_to_no_deployment():
    """Preparing a server and deploying to it are separate decisions."""
    required = "--admin-cidr 203.0.113.8/32 --email operator@example.com"
    assert (
        _query(
            f"parse_options standalone usage_standalone {required}\n"
            'echo "${parsed_deploy}"'
        )
        == "0"
    )
    assert (
        _query(
            f"parse_options standalone usage_standalone {required} --deploy\n"
            'echo "${parsed_deploy}"'
        )
        == "1"
    )
    # Forwarding happens inside a root-only command, so it stays structural.
    assert "handoff_args+=(--deploy)" in _section("standalone")


@pytest.mark.parametrize(
    ("distribution", "version", "accepted"),
    [
        ("ubuntu", "26.04", True),
        # Both of these are supported control-plane platforms and neither can be
        # an edge, which is the whole reason this check is separate from the
        # role's.
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

    It is a control plane and an edge on one machine: the control-plane role
    takes Debian 13+ and Ubuntu 24.04+, the edge play takes Ubuntu 26.04 alone.
    Accepting the wider one here is what made a Debian host install everything
    and then fail at an assert it had no way to anticipate.
    """
    supported = (
        _query(
            f'edge_platform_supported "{distribution}" "{version}" '
            "&& echo yes || echo no"
        )
        == "yes"
    )
    assert supported is accepted


def test_the_platform_is_checked_before_anything_is_installed():
    """Before apt, for the reason --admin-cidr is checked before apt.

    And in `--fresh`, before the confirmation rather than after it: a rebuild
    that cannot reinstall is a destroyed installation, and the platform is
    knowable before the teardown starts.
    """
    standalone = _section("standalone")
    assert standalone.index("require_edge_platform") < standalone.index("apt-get")
    fresh = _section("fresh")
    assert fresh.index("require_edge_platform") < fresh.index("converge_uninstall")
    assert fresh.index("require_edge_platform") < fresh.index("confirm_destructive")
    # Deliberately not the release-move commands: they converge the control
    # plane alone, so refusing them on the platform a host already runs would
    # strand it rather than protect it.
    for command in ("update", "upgrade"):
        assert "require_edge_platform" not in _section(command)


def test_standalone_collects_the_access_list_from_either_spelling():
    """Repeat the flag or hand it a list; the role receives one value.

    Both spellings exist because the setting itself accepts a comma-separated
    string, and an operator who has three offices should not have to remember
    which of the two this script wanted.
    """
    required = "--admin-cidr 203.0.113.8/32 --email operator@example.com"
    assert (
        _query(
            f"parse_options standalone usage_standalone {required} "
            "--allowed-ips 203.0.113.8/32 --allowed-ips 198.51.100.0/24\n"
            'printf "%s\\n" "${parsed_allowed_ips[@]}"'
        )
        == "203.0.113.8/32\n198.51.100.0/24"
    )
    assert (
        _query(
            f"parse_options standalone usage_standalone {required}\n"
            'echo "${#parsed_allowed_ips[@]}"'
        )
        == "0"
    )
    assert (
        _query(
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
        in _section("standalone")
    )


@pytest.mark.parametrize("subcommand", ["update", "upgrade"])
def test_a_release_move_can_repoint_the_access_list(subcommand: str):
    """The three commands that converge the control plane all take the list.

    A server's access list is not a property of the release it is on, so an
    update that could only be run without one would send an operator back to
    editing the environment file by hand for a change they could have made in
    the same breath. Omitting the flag is the common case and leaves the
    installed list untouched.
    """
    section = _section(subcommand)
    assert '--extra-vars "blitzecdn_controlplane_allowed_ips=${allowed_ips}"' in section
    # Before the point of no return, not after it. A rejected access list must
    # cost a refused command, never a host sitting with its services stopped.
    assert section.index("allowed_ips_value") < section.index(
        "stop_control_plane_services"
    )
    assert section.index("allowed_ips_value") < section.index("backup")


def test_the_access_list_survives_an_empty_flag_on_bash_3():
    """macOS ships Bash 3.2, which calls an empty array unbound under `set -u`.

    Every `update` evaluates this whether the flag was given or not, so the
    plain expansion would abort the run that did not ask for an access list at
    all — the overwhelmingly common one.
    """
    assert (
        _query(
            'parse_options update usage_update --yes\nallowed_ips_value\necho "[empty]"'
        )
        == "[empty]"
    )


def test_a_rebuild_carries_the_access_list_with_it():
    """`--fresh` reinstalls exactly as a new server is installed.

    Every standalone option it forwards is one an operator does not have to
    reapply afterwards. An access list left behind would silently return a
    public API to loopback on a rebuild.
    """
    assert (
        _query(
            "parse_options fresh usage_fresh --allowed-ips 203.0.113.8/32 --yes\n"
            'printf "%s\\n" "${parsed_forward_args[@]}"'
        )
        == "--allowed-ips\n203.0.113.8/32"
    )


def test_standalone_guards_existing_sites_from_empty_desired_state():
    """The installer's flag and the variable it ends up as, in one assertion.

    The module is located by importing it rather than by a path written here.
    A path went stale the moment `desired_state.py` moved into `adapters/`,
    and the failure said only that a file was missing — not that the two halves
    of this contract had stopped meeting.
    """
    standalone = _section("standalone")
    assert 'BLITZE_ALLOW_EMPTY_SITES="${parsed_allow_empty_sites}"' in standalone
    assert desired_state.__file__ is not None
    assert "blitzecdn_nginx_allow_empty_sites" in Path(
        desired_state.__file__
    ).read_text(encoding="utf-8")


def test_role_keeps_the_installation_tree_root_owned_and_read_only_to_runtime():
    installation = _role_task("Set the installation directory mode")
    arguments = installation["ansible.builtin.file"]
    assert arguments["owner"] == "root"
    assert arguments["group"] == "root"
    assert arguments["mode"] == "0755"
    assert arguments.get("recurse") is not True


def test_installer_installs_only_third_party_collections():
    """The BlitzeCDN roles ship inside the wheel; nothing pins or builds them.

    The --force that used to be here worked around ansible-core comparing a
    v-prefixed Git ref to a numeric manifest. With no Git-backed collection
    left, needing it again would mean the roles had been re-externalised.
    """
    script = _script()
    assert (
        '-r src/blitzecdn/ansible/requirements.yml -p "${collections_path}"' in script
    )
    assert "--force" not in script
    assert "collection build" not in script


def test_production_bootstrap_does_not_install_the_application_on_the_host():
    script = _script()
    assert "bootstrap_runtime ansible-only" in _section("standalone")
    assert "bootstrap_runtime ansible-only" in _section("update")
    assert "--no-dev --no-install-project" in script


def test_installer_preserves_rather_than_deletes_an_incomplete_virtualenv():
    script = _script()
    # `pip` is no longer the tell: uv builds the environment and a server
    # install has no pip in it at all.
    assert "! -x .venv/bin/python" in script
    assert ".venv.invalid.$(date -u +%Y%m%dT%H%M%SZ)" in script
