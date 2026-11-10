"""Validation primitives shared by two or more parts of the domain.

A shape with one consumer lives with that consumer's contract instead.
"""

from __future__ import annotations

import ipaddress
import re
from fnmatch import fnmatch

from pydantic import BaseModel, ConfigDict

#: One label of a DNS name: no leading or trailing hyphen, 63 bytes at most.
DNS_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

#: A name an operator chooses: a zone's rule, and anything else a person types.
#: Neither dots nor ``*``, because it ends up naming a file.
SITE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

#: A derived virtual host name: may start with a digit, as `9lives.example` can.
HOST_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,126}$")

#: An nginx time value: ``10m``, ``500ms``, ``0s``.
DURATION = re.compile(r"^(?:0|[1-9]\d*)(?:ms|[smhdw])$")


class OmittedWhenEmpty(BaseModel):
    """A nested policy block the edge document leaves out when it holds nothing.

    A capability opts in by subclassing, so core prunes such blocks without naming
    any. Not every block: ``visitor_headers`` has meaningful defaults, so pruning
    it would change what renders.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def empty(self) -> bool:
        return not any(getattr(self, field) for field in type(self).model_fields)


def hostname(value: str, *, wildcard: bool = False) -> str:
    """Normalise a DNS hostname or IP literal, or raise ``ValueError``."""
    candidate = value.strip().lower().rstrip(".")
    if wildcard and candidate.startswith("*."):
        candidate = candidate[2:]
        prefix = "*."
    else:
        prefix = ""
    if len(candidate) > 253 or not candidate:
        raise ValueError("hostname length must be between 1 and 253 characters")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as ip_error:
        if all(DNS_LABEL.fullmatch(label) for label in candidate.split(".")):
            return prefix + candidate
        raise ValueError(f"invalid DNS hostname: {value!r}") from ip_error
    # Outside the `try`, or the `except` above would catch this refusal and
    # accept `*.192.0.2.1` — a `server_name` nginx takes that matches nothing.
    if prefix:
        raise ValueError("wildcards cannot be used with IP addresses")
    _refuse_zone_index(address, value)
    return candidate


def _refuse_zone_index(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, value: str
) -> None:
    # `ipaddress` accepts anything after an IPv6 `%` zone index, and these
    # values reach nginx and the host firewall unquoted: `fe80::1%x;return 200 x`
    # would become a directive. A zone names a local interface, never meaningful
    # here.
    if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
        raise ValueError(f"IPv6 zone indexes are not accepted: {value!r}")


def ip_literal(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse an IP address that will be rendered unquoted, or raise ``ValueError``."""
    address = ipaddress.ip_address(value)
    _refuse_zone_index(address, value)
    return address


def ip_network(
    value: str, *, strict: bool
) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """Parse an IP or CIDR network that will be rendered unquoted.

    ``ValueError`` on anything ``ipaddress.ip_network`` refuses, and on an IPv6
    zone index, which it accepts with arbitrary text after the ``%``.
    """
    network = ipaddress.ip_network(value, strict=strict)
    _refuse_zone_index(network.network_address, value)
    return network


#: "This network" (RFC 1122): Linux routes `0.x` to the host itself. Python's
#: ``is_unspecified`` covers only `0.0.0.0`.
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")

#: Cloud metadata services outside link-local, which ``is_link_local`` catches
#: (`169.254.169.254`): AWS's IPv6 endpoint is a unique local address and
#: Alibaba's sits in shared address space, both otherwise allowed on opt-in.
_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(address) for address in ("fd00:ec2::254", "100.100.100.200")
)

#: Networks an origin may sit in only when its site opts in: RFC 1918, shared
#: address space (RFC 6598, often a provider's internal network) and IPv6
#: unique local addresses. Spelled out rather than ``is_private``, which also
#: holds the documentation ranges and the ones refused outright below.
PRIVATE_ORIGIN_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "fc00::/7",
    )
)


def _origin_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address an origin literal names, an IPv4-mapped IPv6 one unwrapped."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def refuse_internal_origin(host: str) -> None:
    """Refuse an origin that would point an edge at itself; ``ValueError`` if so.

    Edges run nginx in the host's network namespace, so a loopback origin
    reaches the edge's own services and a link-local one reaches the cloud
    metadata service and its credentials. No site has a legitimate reason to
    proxy to either, nor to an unspecified, multicast or reserved address, so
    no opt-in exists.

    Only IP literals are checked. A hostname is resolved by the edge at
    request time and can rebind to any of these. Proxied records carry
    addresses, so a release never holds one, but anything that hands an edge
    a named origin owns that risk.
    """
    address = _origin_ip(host)
    if address is None:
        return
    kind = (
        "unspecified"
        if address.is_unspecified or address in _THIS_NETWORK
        else "loopback"
        if address.is_loopback
        else "link-local"
        if address.is_link_local
        or (isinstance(address, ipaddress.IPv6Address) and address.is_site_local)
        else "cloud metadata"
        if address in _METADATA_ADDRESSES
        else "multicast"
        if address.is_multicast
        else "reserved"
        if address.is_reserved
        else None
    )
    if kind is not None:
        raise ValueError(
            f"origin {host} is a {kind} address, which reaches the edge itself "
            "rather than a website; proxy to the origin's public address"
        )


def is_private_origin(host: str) -> bool:
    """Whether ``host`` is an address in :data:`PRIVATE_ORIGIN_NETWORKS`."""
    address = _origin_ip(host)
    return address is not None and any(
        address in network
        for network in PRIVATE_ORIGIN_NETWORKS
        if network.version == address.version
    )


def unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    """Refuse a repeated entry rather than silently collapsing it."""
    deduplicated = tuple(dict.fromkeys(values))
    if len(deduplicated) != len(values):
        raise ValueError(f"duplicate {label} entries are not allowed")
    return deduplicated


#: Host patterns a deploy may narrow itself to, comma-separated. Ansible's
#: ``:``, ``&``, ``!`` and ``@`` are absent so a limit can only narrow.
EDGE_LIMIT = re.compile(r"^[A-Za-z0-9_.*-]+(?:,[A-Za-z0-9_.*-]+)*$")
_MAX_LIMIT_LENGTH = 512


def validate_edge_limit(value: str | None) -> str | None:
    """Normalise a deploy host limit, or raise ``ValueError``."""
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if len(candidate) > _MAX_LIMIT_LENGTH:
        raise ValueError(f"host limit must be at most {_MAX_LIMIT_LENGTH} characters")
    if not EDGE_LIMIT.fullmatch(candidate):
        raise ValueError(
            "host limit must be a comma-separated list of edge names or globs "
            "using only letters, digits, '.', '_', '-' and '*'. Ansible's ':', "
            "'&', '!' and '@' patterns are refused: a limit may only narrow a "
            "deploy, never widen it."
        )
    return candidate


def matches_edge_limit(name: str, host_limit: str) -> bool:
    """Whether one edge name is inside a validated host limit.

    The one matcher, so Ansible's targets and the release compiler's capability
    check agree on what a canary is. Terms are a union matched with :mod:`fnmatch`.
    """
    return any(fnmatch(name, pattern) for pattern in host_limit.split(","))


#: Variables the fleet-wide settings may never set: each is derived per host,
#: and settings are published at host precedence, so one would override every
#: edge — ``blitzecdn_firewall_ssh_port`` could lock the controller out.
RESERVED_ANSIBLE_SETTINGS = frozenset(
    {
        "blitzecdn_firewall_ssh_port",
        "blitzecdn_firewall_ssh_sources",
        "blitzecdn_public_addresses",
        "blitzecdn_nginx_sites",
    }
)

#: Substrings that mark a name as carrying a credential rather than policy.
_SECRET_WORDS = ("password", "secret", "token", "key")

#: Required, so a setting cannot reach `ansible_host` or other connection vars.
_SETTING_PREFIX = "blitzecdn_"


def validate_setting_name(value: str) -> str:
    """Normalise a fleet-wide Ansible setting name, or raise ``ValueError``."""
    candidate = value.strip()
    if not candidate.startswith(_SETTING_PREFIX):
        raise ValueError(f"setting names must start with {_SETTING_PREFIX!r}")
    if candidate in RESERVED_ANSIBLE_SETTINGS:
        raise ValueError(f"{candidate} is derived from edge or desired-state records")
    if any(word in candidate.lower() for word in _SECRET_WORDS):
        raise ValueError("secrets and keys belong in .env, not the database")
    return candidate
