"""Address filters for the edge firewall role.

Filters run on the controller, so these use the standard library rather than
ansible.utils.ipaddr, which would add a collection and its netaddr dependency
for one membership test.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterable
from typing import Any


def address_in_networks(address: str, networks: Iterable[str]) -> bool:
    """Whether ``address`` lies inside any of ``networks``.

    An IPv4 client seen through an IPv6 socket arrives as ``::ffff:a.b.c.d``;
    it is the IPv4 address that ufw matches, so that is the one compared. An
    address or network that does not parse matches nothing: the caller is a
    guard, and a guard that cannot tell must refuse.
    """
    try:
        parsed = ipaddress.ip_address(str(address).strip())
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    for network in networks:
        try:
            if parsed in ipaddress.ip_network(str(network).strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


class FilterModule:
    """Ansible's entry point for role-local filters."""

    def filters(self) -> dict[str, Callable[..., Any]]:
        return {"blitzecdn_address_in_networks": address_in_networks}
