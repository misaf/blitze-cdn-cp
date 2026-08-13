"""An edge server, as the control plane records it.

This is the model the Ansible inventory is *derived from*. There is no
inventory file: the ``blitzecdn`` inventory plugin reads the edges table at the
start of every run, so a host exists for Ansible exactly when it exists here.

One table and a plugin that reads it is the whole mechanism, and the point of
it is that there is nowhere else a host can be declared — nothing to edit
underneath the control plane, and no file left holding an edge the database has
already decommissioned.

Everything here is provider-independent and does no I/O. ``to_inventory``
renders the host the way the plugin publishes it, and is the whole of the
mapping between this model and Ansible's connection variables.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.domain.validation import hostname, is_stored, unique

#: The version of the ``edges`` row shape the inventory plugin reads.
#:
#: Bump it when ``Edge`` changes in a way an older plugin could not honour — a
#: renamed field, or new semantics for an existing one. Adding a field the
#: plugin can ignore does not need a bump. The plugin refuses a version above
#: the one it knows, so an upgraded control plane and a stale checkout of the
#: Ansible tree fail loudly at the start of a run instead of converging a fleet
#: the plugin only half understood.

#: The Ansible group every managed edge belongs to. The playbooks target it by
#: name, so it is part of the contract with the roles rather than a label.
EDGE_GROUP = "blitzecdn_edges"

#: A stable edge name, which becomes the Ansible inventory hostname.
#:
#: Dots are allowed because an operator naturally names an edge after its FQDN
#: (``edge-01.example.net``), and Ansible is happy with that. Whitespace,
#: commas and glob characters are not: the name is joined with commas into
#: ``--limit`` and matched with ``fnmatch`` there, so a name carrying ``,``,
#: ``*`` or ``?`` could widen a canary into a fleet-wide run.
EDGE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")


class Edge(BaseModel):
    """One edge server: how to reach it, and what it answers on publicly.

    The two address concepts are deliberately separate. ``host`` is how the
    *controller* connects — often a management name or a bastion-reachable
    address — while ``public_addresses`` are what the *world* reaches for CDN
    traffic. On a NAT'd or multi-homed edge these differ, and conflating them
    breaks certificate preflight: it checks that a customer's hostname resolves
    to one of our edges, which is a question about the public side only.
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
    #: Empty means "the same address the controller connects to", which is the
    #: common single-homed case. Resolved at use rather than stored expanded,
    #: so a renumbered edge is one update.
    public_addresses: tuple[str, ...] = Field(default=(), max_length=20)
    #: Management networks permitted to reach SSH on the fleet. Recorded per
    #: edge because that is where an operator naturally supplies it, and
    #: published as the union across every edge — see ``firewall_sources``.
    ssh_sources: tuple[str, ...] = Field(default=(), max_length=100)

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

    @field_validator("host", "user")
    @classmethod
    def validate_connection_field(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("cannot be empty or contain whitespace")
        return normalized

    @field_validator("private_key_file")
    @classmethod
    def validate_private_key_file(cls, value: str | None) -> str | None:
        """A path, not an argument.

        This is handed to SSH as ``ansible_ssh_private_key_file``. Whitespace
        would let one value become two arguments, so it is refused here rather
        than discovered as a connection failure with no obvious cause. ``~`` is
        left alone: Ansible expands it, and rejecting it would refuse the
        spelling every operator actually uses.
        """
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("private key path cannot be empty or contain whitespace")
        return normalized

    @field_validator("public_addresses")
    @classmethod
    def validate_public_addresses(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Each entry is an address or a resolvable name, and they are distinct.

        Not required to be an IP: an edge behind a name that resolves to
        several addresses is a normal deployment, and preflight resolves these
        anyway before comparing them.
        """
        normalized = []
        for item in values:
            candidate = item.strip()
            if not candidate or any(char.isspace() for char in candidate):
                raise ValueError(
                    "public addresses cannot be empty or contain whitespace"
                )
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                candidate = hostname(candidate)
            normalized.append(candidate)
        return unique(tuple(normalized), "public address")

    @field_validator("ssh_sources")
    @classmethod
    def validate_ssh_sources(
        cls, values: tuple[str, ...], info: Any
    ) -> tuple[str, ...]:
        """Normalise to CIDR.

        ``strict=False`` here, unlike the site firewall's source rules, because
        an operator supplying a management network types their own address —
        ``--ssh-source 203.0.113.8/24`` means "my /24", and refusing it would
        be pedantry about a value that is only ever widened to its network.
        The site firewall refuses the same thing because there a silent widening
        turns a rule written for one address into one covering 256.
        """
        normalized = []
        for item in values:
            candidate = item.strip()
            try:
                normalized.append(str(ipaddress.ip_network(candidate, strict=False)))
            except ValueError as error:
                if is_stored(info):
                    normalized.append(candidate)
                    continue
                raise ValueError(
                    f"{item!r} is not a management CIDR such as '203.0.113.0/24': "
                    f"{error}"
                ) from error
        return unique(tuple(normalized), "management CIDR")

    @model_validator(mode="after")
    def validate_public_addresses_are_not_the_host(self) -> Self:
        """A single public address equal to ``host`` is the default; drop it.

        Storing it would make two spellings of the same edge — one with the
        field set, one without — that ``list_edges`` reports identically. The
        redundant one then survives a change of ``host`` and keeps pointing at
        the old address.
        """
        if self.public_addresses == (self.host,):
            object.__setattr__(self, "public_addresses", ())
        return self

    @property
    def effective_public_addresses(self) -> tuple[str, ...]:
        """What the world reaches, falling back to the management address.

        The fallback is the single-homed case, which is most of them. Preflight
        and the DNS guidance both ask this rather than reading the field, so
        neither has to know the empty tuple means "same as host".
        """
        return self.public_addresses or (self.host,)

    def to_inventory(self) -> dict[str, Any]:
        """The Ansible host variables the inventory plugin publishes.

        The whole mapping between this model and Ansible lives here, so the
        plugin stays a thin reader: it selects rows, builds these dictionaries,
        and adds them to the group. Nothing is emitted that is unset, because a
        ``None`` connection variable is not the same as an absent one — Ansible
        would hand SSH an empty identity file rather than letting it resolve a
        key the usual way.
        """
        variables: dict[str, Any] = {
            "ansible_host": self.host,
            "ansible_user": self.user,
            "ansible_port": self.port,
            # Published from here so the firewall cannot disagree with the port
            # Ansible actually connects on. Two independent settings would
            # strand the host on any mismatch: the
            # firewall closes the port the next converge arrives on, and no
            # later deploy can reach it to undo that.
            "blitzecdn_firewall_ssh_port": self.port,
        }
        if self.private_key_file is not None:
            variables["ansible_ssh_private_key_file"] = self.private_key_file
        if self.public_addresses:
            variables["blitzecdn_public_addresses"] = list(self.public_addresses)
        return variables


class EdgePatch(BaseModel):
    """A partial update to an edge: unset means untouched.

    ``name`` is absent by design — see ``EdgeOperationsService.update_edge``.
    Every other field is optional, and the merged result is revalidated through
    :class:`Edge`, so a patch cannot install a value the model would refuse.

    ``public_addresses`` and ``ssh_sources`` replace their list wholesale
    rather than merging, for the same reason the site firewall block does:
    merging makes "remove the last entry" impossible to express.
    """

    model_config = ConfigDict(extra="forbid")

    host: str | None = None
    user: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    private_key_file: str | None = None
    public_addresses: tuple[str, ...] | None = None
    ssh_sources: tuple[str, ...] | None = None


def firewall_sources(edges: list[Edge]) -> list[str]:
    """The management CIDRs published to every edge, as one union.

    Every edge is given the union rather than only its own sources. The set is
    what an operator has declared to be "our management network", and narrowing
    it per host would mean an edge registered before a second office existed
    quietly refuses the operator sitting in it — a lockout discovered at the
    worst moment, during an incident, on the host being repaired.

    Published as a *host* variable by the plugin. It is the same value for
    every host, so a group variable would express it more naturally, but
    ``inventory/group_vars/blitzecdn_edges/defaults.yml`` also sets this key and
    an inventory group variable loses to it. Host variables outrank both, which
    is what makes ``edge add --ssh-source`` take effect at all.
    """
    return sorted(
        {source for edge in edges for source in edge.ssh_sources},
        key=_source_order,
    )


def _source_order(source: str) -> tuple[int, Any]:
    """Sort IPv4 before IPv6 and numerically within each, not as text.

    Lexical order puts ``203.0.113.0/24`` before ``94.183.119.90/32``, which
    reads as a mistake in a rendered firewall rule and in the diff of a deploy.
    """
    network = ipaddress.ip_network(source, strict=False)
    return (network.version, network)


__all__ = [
    "EDGE_GROUP",
    "EDGE_NAME",
    "Edge",
    "EdgePatch",
    "firewall_sources",
]
