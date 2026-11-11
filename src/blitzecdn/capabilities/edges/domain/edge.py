"""An edge server, as the control plane records it.

The Ansible inventory is derived from this: the inventory plugin reads the
edges table each run, so there is nowhere else a host can be declared.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.core.domain.validation import hostname, ip_literal, unique
from blitzecdn.core.domain.values import IpNetworkWidened

#: The Ansible group every managed edge belongs to. The playbooks target it by
#: name, so it is part of the contract with the roles rather than a label.
EDGE_GROUP = "blitzecdn_edges"

#: A stable edge name, the Ansible inventory hostname. Dots are fine; commas,
#: whitespace and globs are not, since names join into an ``fnmatch`` ``--limit``
#: and could widen a canary into the fleet.
EDGE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")

#: A login name for ``ansible_user``: the portable POSIX set, 32 bytes at most.
#: It reaches the SSH command line, so it may not start with ``-``, and ``@``
#: or ``:`` could redirect the connection.
EDGE_USER = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9._-]{0,31}$")


class Edge(BaseModel):
    """One edge server: how to reach it, and what it answers on publicly.

    ``host`` is how the controller connects; ``public_addresses`` are what the
    world reaches. They differ behind NAT, and preflight asks about the public side.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    #: What the controller connects to. Becomes ``ansible_host``.
    host: str
    #: Becomes ``ansible_user``. Never root — the roles escalate per task.
    user: str = "deploy"
    #: Becomes ``ansible_port``. Must match ``blitzecdn_firewall_ssh_port`` on
    #: the edge, or the firewall closes the port the next converge arrives on.
    port: int = Field(default=22, ge=1, le=65535)
    #: Becomes ``ansible_ssh_private_key_file``. Left unset, SSH resolves the
    #: key the usual way (agent, ``~/.ssh/config``, default identities).
    private_key_file: str | None = None
    #: Empty means the controller's address; resolved at use.
    public_addresses: tuple[str, ...] = Field(default=(), max_length=20)
    #: Management networks allowed to reach SSH; published as a union across edges.
    ssh_sources: tuple[IpNetworkWidened, ...] = Field(default=(), max_length=100)
    #: Capability tokens this edge's runtime is declared to provide; ``None`` means
    #: the controller's set. Declared, so compilation can refuse a site before a run;
    #: the edge role still probes the real binary at converge time.
    capabilities: tuple[str, ...] | None = Field(default=None, max_length=50)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not EDGE_NAME.fullmatch(normalized):
            raise ValueError(
                "edge name must start with a letter or digit and contain only "
                "letters, digits, dots, hyphens and underscores"
            )
        return normalized

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        """A DNS hostname or IP literal, nothing else.

        It becomes ``ansible_host``, which Ansible puts on the ``ssh`` command
        line without ``--``, and the host pattern recorded in ``known_hosts``:
        ``-oProxyCommand=…`` would be an option there, and ``*`` or ``a,*``
        would trust one edge's key for every host.
        """
        return hostname(value)

    @field_validator("user")
    @classmethod
    def validate_user(cls, value: str) -> str:
        normalized = value.strip()
        if not EDGE_USER.fullmatch(normalized):
            raise ValueError(
                "user must be at most 32 characters, start with a letter, digit "
                "or underscore, and contain only letters, digits, dots, hyphens "
                "and underscores"
            )
        return normalized

    @field_validator("private_key_file")
    @classmethod
    def validate_private_key_file(cls, value: str | None) -> str | None:
        """A path, not an argument: no whitespace reaches SSH; ``~`` is fine."""
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("private key path cannot be empty or contain whitespace")
        return normalized

    @field_validator("public_addresses")
    @classmethod
    def validate_public_addresses(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Each entry an address or resolvable name, all distinct."""
        normalized = []
        for item in values:
            candidate = item.strip()
            if not candidate or any(char.isspace() for char in candidate):
                raise ValueError(
                    "public addresses cannot be empty or contain whitespace"
                )
            try:
                ip_literal(candidate)
            except ValueError:
                candidate = hostname(candidate)
            normalized.append(candidate)
        return unique(tuple(normalized), "public address")

    @field_validator("ssh_sources")
    @classmethod
    def validate_ssh_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Distinct management CIDRs.

        Host bits are widened, unlike the site firewall: ``203.0.113.8/24`` here
        means "my /24", an operator naming their management network.
        """
        return unique(values, "management CIDR")

    @model_validator(mode="after")
    def validate_public_addresses_are_not_the_host(self) -> Self:
        """Drop a sole public address equal to ``host``; it would go stale."""
        if self.public_addresses == (self.host,):
            object.__setattr__(self, "public_addresses", ())
        return self

    @property
    def effective_public_addresses(self) -> tuple[str, ...]:
        """What the world reaches, falling back to the management address."""
        return self.public_addresses or (self.host,)


class EdgePatch(BaseModel):
    """A partial update to an edge; the name is fixed and the merge is revalidated.

    The two lists replace wholesale, so removing the last entry is expressible.
    """

    model_config = ConfigDict(extra="forbid")

    host: str | None = None
    user: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    private_key_file: str | None = None
    public_addresses: tuple[str, ...] | None = None
    ssh_sources: tuple[str, ...] | None = None
    capabilities: tuple[str, ...] | None = Field(default=None, max_length=50)


def firewall_sources(edges: list[Edge]) -> list[str]:
    """The management CIDRs published to every edge, as one union.

    The union, so an edge registered earlier never locks out a newer office mid
    incident. A host variable, because the shipped group defaults would beat an
    inventory group variable.
    """
    return sorted(
        {source for edge in edges for source in edge.ssh_sources},
        key=_source_order,
    )


def _source_order(source: str) -> tuple[int, Any]:
    """Sort IPv4 before IPv6, numerically, so rendered rules read naturally."""
    network = ipaddress.ip_network(source, strict=False)
    return (network.version, network)


__all__ = [
    "EDGE_GROUP",
    "EDGE_NAME",
    "EDGE_USER",
    "Edge",
    "EdgePatch",
    "firewall_sources",
]
