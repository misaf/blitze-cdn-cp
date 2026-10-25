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
    # `ip_address` accepts anything after an IPv6 `%` zone index, and these
    # values reach nginx unquoted: `fe80::1%x;return 200 x` would become a
    # directive. A zone names a local interface, never meaningful here.
    if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
        raise ValueError(f"IPv6 zone indexes are not accepted: {value!r}")
    return candidate


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
