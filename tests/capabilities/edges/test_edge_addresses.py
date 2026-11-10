"""An edge's addresses and SSH sources reach the host firewall and nginx unquoted."""

import pytest
from pydantic import ValidationError

from blitzecdn.capabilities.edges.domain.edge import Edge


def edge(**fields) -> Edge:
    return Edge(name="edge-1", host="192.0.2.10", **fields)


def test_ssh_sources_are_widened_to_their_network():
    assert edge(ssh_sources=("203.0.113.8/24",)).ssh_sources == ("203.0.113.0/24",)


@pytest.mark.parametrize("source", ["fe80::1%x;return 200 x", "fe80::%eth0/64"])
def test_an_ssh_source_with_an_ipv6_zone_index_is_refused(source):
    with pytest.raises(ValidationError, match="zone indexes"):
        edge(ssh_sources=(source,))


def test_a_public_address_with_an_ipv6_zone_index_is_refused():
    with pytest.raises(ValidationError, match="zone indexes"):
        edge(public_addresses=("2001:db8::1%x;return",))


def test_a_public_address_may_be_an_ip_or_a_name():
    assert edge(public_addresses=("2001:db8::1", "edge.example.")).public_addresses == (
        "2001:db8::1",
        "edge.example",
    )
