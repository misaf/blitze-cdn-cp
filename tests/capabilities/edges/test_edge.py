"""The edge record refuses connection values that would reach SSH as something else."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from blitzecdn.capabilities.edges.domain.edge import Edge


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("192.0.2.10", "192.0.2.10"),
        ("2001:db8::1", "2001:db8::1"),
        ("Edge-01.Example.com.", "edge-01.example.com"),
        ("localhost", "localhost"),
    ],
)
def test_host_is_a_hostname_or_ip_literal(value, expected):
    assert Edge(name="e1", host=value).host == expected


@pytest.mark.parametrize(
    "value",
    [
        # An SSH option: Ansible puts `ansible_host` on argv without `--`.
        "-oProxyCommand=id",
        # known_hosts patterns that would trust the key for other hosts.
        "*",
        "a,*",
        "edge?.example.com",
        "!edge.example.com",
        # A user or port smuggled into the host.
        "root@192.0.2.10",
        "192.0.2.10:2222",
        "[192.0.2.10]:2222",
        # An IPv6 zone index names a local interface.
        "fe80::1%eth0",
        "",
        "edge one",
    ],
)
def test_host_refuses_anything_else(value):
    with pytest.raises(ValidationError):
        Edge(name="e1", host=value)


@pytest.mark.parametrize("value", ["deploy", "ubuntu", "_svc", "ci.deploy-2"])
def test_user_accepts_a_login_name(value):
    assert Edge(name="e1", host="192.0.2.10", user=value).user == value


@pytest.mark.parametrize(
    "value",
    ["-oProxyCommand=id", "root@evil", "a:b", "a,b", "*", "", "de ploy", "u" * 33],
)
def test_user_refuses_anything_else(value):
    with pytest.raises(ValidationError):
        Edge(name="e1", host="192.0.2.10", user=value)
