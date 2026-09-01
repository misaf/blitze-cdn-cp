"""Validation primitives the rest of the domain is built from.

Everything here is shared by two or more of the sibling modules — hostnames by
sites, DNS and certificates. Nothing here knows what a site or a record is,
which is what keeps this module free of imports from its own package.

That first sentence is the rule rather than a description of the contents.
Country codes, HTTP method shapes and the alias table lived here with exactly
one consumer — :mod:`blitzecdn.features.security.policy` — which left core
carrying the vocabulary of a capability an operator can detach. They now live
with the contract that validates against them.
"""

from __future__ import annotations

import ipaddress
import re

from pydantic import BaseModel, ConfigDict

#: One label of a DNS name: no leading or trailing hyphen, 63 bytes at most.
DNS_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

#: An internal site name. Neither dots nor ``*``, because it names a file.
SITE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

#: An nginx time value: ``10m``, ``500ms``, ``0s``.
DURATION = re.compile(r"^(?:0|[1-9]\d*)(?:ms|[smhdw])$")


class OmittedWhenEmpty(BaseModel):
    """A nested policy block the edge document leaves out when it holds nothing.

    Opting in by subclassing, rather than core inspecting every nested model,
    is the point. :func:`~blitzecdn.core.ansible.mapping.site_to_ansible` used
    to name one block — ``firewall`` — which put the vocabulary of a detachable
    capability into a generic adapter and meant a second such block would be a
    second branch there. A capability now declares that its block is absent
    rather than empty in the document, and core prunes whatever declares it
    without knowing what any of them contain.

    It is deliberately not every nested block. ``visitor_headers`` carries
    switches whose *default* is meaningful to the role, so a block that has
    never been configured is not the same as one that is off, and pruning it
    would change what the edge renders.
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
        ipaddress.ip_address(candidate)
    except ValueError as ip_error:
        if all(DNS_LABEL.fullmatch(label) for label in candidate.split(".")):
            return prefix + candidate
        raise ValueError(f"invalid DNS hostname: {value!r}") from ip_error
    # Deliberately outside the `try`. Raising this inside it would have the
    # `except` above catch our own refusal, and the label check there accepts an
    # IPv4 literal — every label of `192.0.2.1` is a valid DNS label — so
    # `*.192.0.2.1` came back normalised rather than rejected. nginx takes such
    # a `server_name` without complaint and it then matches no request ever
    # sent, which is the same silent-no-op failure that the security
    # capability's assigned-country-code check exists to prevent.
    if prefix:
        raise ValueError("wildcards cannot be used with IP addresses")
    return candidate


def unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    """Refuse a repeated entry rather than silently collapsing it."""
    deduplicated = tuple(dict.fromkeys(values))
    if len(deduplicated) != len(values):
        raise ValueError(f"duplicate {label} entries are not allowed")
    return deduplicated


#: Host patterns a deploy may narrow itself to, as a comma-separated list.
#:
#: Deliberately narrower than Ansible's own pattern syntax. ``:`` (union),
#: ``&`` (intersection), ``!`` (exclusion) and ``@`` (read hosts from a file)
#: are all absent, because a limit must only ever be able to *narrow* a
#: deploy. ``AnsibleRunner._limit`` then expands whatever passes this against
#: the edges the inventory actually declares, so the two together mean a limit
#: can never reach a host a full deploy would not have reached.
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


#: Variables the fleet-wide settings table may never set.
#:
#: Each is derived per host from the edge record or from desired state, and the
#: inventory plugin publishes settings at *host* precedence — so a row named
#: like one of these would win over the derived value for every edge at once.
#: ``blitzecdn_firewall_ssh_port`` is the dangerous one: the firewall would
#: close the port the next converge arrives on, and no later deploy could reach
#: the host to put it back.
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

#: The prefix every role variable this control plane owns already carries.
#: Requiring it is what keeps a setting from reaching `ansible_host`,
#: `ansible_user` or any other connection variable the inventory derives.
_SETTING_PREFIX = "blitzecdn_"


def validate_setting_name(value: str) -> str:
    """Normalise a fleet-wide Ansible setting name, or raise ``ValueError``.

    These rules used to live in the Typer callback behind ``blitzecdn config
    set``, which made them a property of one entry point rather than of the
    setting. Everything downstream — the store, and the inventory plugin that
    publishes these rows to every host — behaved as though they had been
    applied, so any second writer would have bypassed them silently.
    """
    candidate = value.strip()
    if not candidate.startswith(_SETTING_PREFIX):
        raise ValueError(f"setting names must start with {_SETTING_PREFIX!r}")
    if candidate in RESERVED_ANSIBLE_SETTINGS:
        raise ValueError(f"{candidate} is derived from edge or desired-state records")
    if any(word in candidate.lower() for word in _SECRET_WORDS):
        raise ValueError("secrets and keys belong in .env, not the database")
    return candidate
