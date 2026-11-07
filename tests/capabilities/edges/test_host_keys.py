"""Edge SSH host keys: fingerprints, and the file the controller trusts them in."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from support.host_keys import FIRST, FIRST_FINGERPRINT, SECOND, SECOND_FINGERPRINT

import blitzecdn.capabilities.edges.adapters.host_keys as host_keys_module
from blitzecdn.capabilities.edges.adapters.host_keys import KnownHosts
from blitzecdn.capabilities.edges.domain import (
    known_hosts_pattern,
    normalize_fingerprint,
    select_host_keys,
)
from blitzecdn.core.exceptions import ConfigurationError, ExecutionError


def test_fingerprint_is_the_one_ssh_keygen_prints():
    assert FIRST.fingerprint == FIRST_FINGERPRINT
    assert SECOND.fingerprint == SECOND_FINGERPRINT


def test_a_non_standard_port_is_bracketed_as_ssh_looks_it_up():
    assert known_hosts_pattern("192.0.2.10", 22) == "192.0.2.10"
    assert known_hosts_pattern("192.0.2.10", 7845) == "[192.0.2.10]:7845"


def test_a_fingerprint_is_accepted_with_or_without_its_prefix():
    bare = FIRST_FINGERPRINT.removeprefix("SHA256:")
    assert normalize_fingerprint(bare) == FIRST_FINGERPRINT
    assert normalize_fingerprint(f" {FIRST_FINGERPRINT} ") == FIRST_FINGERPRINT


@pytest.mark.parametrize(
    "value",
    [
        "MD5:16:27:ac:a5:76:28:2d:36:63:1b:56:4d:eb:df:a6:48",
        "16:27:ac:a5:76:28:2d:36:63:1b:56:4d:eb:df:a6:48",
        "SHA256:short",
        "not a fingerprint",
    ],
)
def test_anything_but_a_sha256_fingerprint_is_refused(value):
    with pytest.raises(ConfigurationError):
        normalize_fingerprint(value)


def test_only_the_vouched_for_key_is_kept():
    """Another key the host offered is not vouched for, so it is not trusted."""
    assert select_host_keys((FIRST, SECOND), [SECOND_FINGERPRINT]) == (SECOND,)


def test_a_fingerprint_matching_nothing_refuses_every_key():
    with pytest.raises(ConfigurationError, match="intercepted"):
        select_host_keys((FIRST,), [SECOND_FINGERPRINT])


# --- the known_hosts file ----------------------------------------------------


@pytest.fixture
def known_hosts(tmp_path) -> KnownHosts:
    return KnownHosts(SimpleNamespace(state_dir=tmp_path))  # type: ignore[arg-type]


def test_keys_are_managed_only_where_the_controller_configures_ssh(
    known_hosts, tmp_path
):
    """A checkout's SSH reads the operator's known_hosts, not this one."""
    assert not known_hosts.managed
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh/config").write_text("", encoding="utf-8")
    assert known_hosts.managed


def test_trusting_replaces_that_host_and_keeps_every_other_line(known_hosts):
    known_hosts.path.parent.mkdir()
    known_hosts.path.write_text(
        "# operator note\n"
        f"192.0.2.10 {FIRST.key_type} {FIRST.key}\n"
        f"192.0.2.11,edge-02 {FIRST.key_type} {FIRST.key}\n"
        f"[192.0.2.10]:7845 {FIRST.key_type} {FIRST.key}\n"
        f"|1|c2FsdA==|aGFzaA== {FIRST.key_type} {FIRST.key}\n"
        f"@revoked * {SECOND.key_type} {SECOND.key}\n",
        encoding="utf-8",
    )

    known_hosts.trust("192.0.2.10", 22, (SECOND,))

    assert known_hosts.trusted("192.0.2.10", 22) == (SECOND,)
    assert known_hosts.trusted("192.0.2.11", 22) == (FIRST,)
    assert known_hosts.trusted("192.0.2.10", 7845) == (FIRST,)
    lines = known_hosts.path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# operator note"
    assert any(line.startswith("|1|") for line in lines)
    assert any(line.startswith("@revoked") for line in lines)
    assert known_hosts.path.stat().st_mode & 0o777 == 0o600


#: `192.0.2.10 <FIRST>` as `ssh-keygen -H` hashed it.
HASHED_EDGE = "|1|UOguBdvTgVNFUgY+rE64teNLRK8=|+hji3LwMPDo7yAdvFP5747mSCH8="


def test_a_hashed_entry_for_the_host_is_replaced_too(known_hosts):
    """SSH would still accept a hashed entry's key, so a rotation must remove it."""
    known_hosts.path.parent.mkdir()
    known_hosts.path.write_text(
        f"{HASHED_EDGE} {FIRST.key_type} {FIRST.key}\n"
        f"|1|c2FsdA==|aGFzaA== {FIRST.key_type} {FIRST.key}\n",
        encoding="utf-8",
    )
    assert known_hosts.trusted("192.0.2.10", 22) == (FIRST,)

    known_hosts.trust("192.0.2.10", 22, (SECOND,))

    assert known_hosts.trusted("192.0.2.10", 22) == (SECOND,)
    text = known_hosts.path.read_text(encoding="utf-8")
    assert HASHED_EDGE not in text
    assert "|1|c2FsdA==|aGFzaA==" in text


def test_a_shared_line_keeps_trusting_its_other_names(known_hosts):
    """Rotating one address must not drop an alias's trust in the old key."""
    known_hosts.path.parent.mkdir()
    known_hosts.path.write_text(
        f"192.0.2.10,edge-01 {FIRST.key_type} {FIRST.key} a comment\n",
        encoding="utf-8",
    )

    known_hosts.trust("192.0.2.10", 22, (SECOND,))

    assert known_hosts.trusted("192.0.2.10", 22) == (SECOND,)
    assert known_hosts.trusted("edge-01", 22) == (FIRST,)
    assert (
        f"edge-01 {FIRST.key_type} {FIRST.key} a comment"
        in known_hosts.path.read_text(encoding="utf-8").splitlines()
    )


def test_trusting_a_first_host_creates_the_file(known_hosts):
    known_hosts.trust("192.0.2.10", 7845, (FIRST,))

    assert known_hosts.path.read_text(encoding="utf-8") == (
        f"[192.0.2.10]:7845 {FIRST.key_type} {FIRST.key}\n"
    )


def test_a_scan_reads_only_the_keys_for_the_scanned_address(known_hosts, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                f"# 192.0.2.10:7845 SSH-2.0-OpenSSH_9.9\n"
                f"[192.0.2.10]:7845 {FIRST.key_type} {FIRST.key}\n"
                f"[192.0.2.10]:7845 {SECOND.key_type} {SECOND.key}\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(host_keys_module.subprocess, "run", run)

    assert known_hosts.scan("192.0.2.10", 7845) == (FIRST, SECOND)
    assert calls[0][-2:] == ["--", "192.0.2.10"]
    assert calls[0][calls[0].index("-p") + 1] == "7845"


def test_a_host_offering_no_key_is_an_error(known_hosts, monkeypatch):
    monkeypatch.setattr(
        host_keys_module.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="connect to 192.0.2.10 port 22: timed out\n"
        ),
    )

    with pytest.raises(ExecutionError, match="timed out"):
        known_hosts.scan("192.0.2.10", 22)
