"""Validation primitives the rest of the domain is built from.

Everything here is shared by two or more of the sibling modules — hostnames by
sites, DNS and certificates; the stored-record escape hatch by anything read
back out of persistence. Nothing here knows what a site or a record is, which
is what keeps this module free of imports from its own package.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

#: One label of a DNS name: no leading or trailing hyphen, 63 bytes at most.
DNS_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

#: An internal site name. Neither dots nor ``*``, because it names a file.
SITE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

#: An nginx time value: ``10m``, ``500ms``, ``0s``.
DURATION = re.compile(r"^(?:0|[1-9]\d*)(?:ms|[smhdw])$")

COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")

HTTP_METHOD = re.compile(r"^[A-Z][A-Z-]{1,19}$")

#: ISO 3166-1 alpha-2, which is what the MaxMind database emits as `iso_code`.
#:
#: Checked against the real list rather than the two-letter shape alone,
#: because a plausible-looking code that is not assigned produces the worst
#: outcome this feature has: `--allow-country UK` validates, deploys, renders,
#: and then matches nothing, because the United Kingdom is GB. The rule looks
#: enforced and blocks nobody. A shape check cannot tell those apart.
# fmt: off
ISO_3166_1_ALPHA_2 = frozenset((
    "AD", "AE", "AF", "AG", "AI", "AL", "AM", "AO", "AQ", "AR", "AS", "AT", "AU",
    "AW", "AX", "AZ", "BA", "BB", "BD", "BE", "BF", "BG", "BH", "BI", "BJ", "BL",
    "BM", "BN", "BO", "BQ", "BR", "BS", "BT", "BV", "BW", "BY", "BZ", "CA", "CC",
    "CD", "CF", "CG", "CH", "CI", "CK", "CL", "CM", "CN", "CO", "CR", "CU", "CV",
    "CW", "CX", "CY", "CZ", "DE", "DJ", "DK", "DM", "DO", "DZ", "EC", "EE", "EG",
    "EH", "ER", "ES", "ET", "FI", "FJ", "FK", "FM", "FO", "FR", "GA", "GB", "GD",
    "GE", "GF", "GG", "GH", "GI", "GL", "GM", "GN", "GP", "GQ", "GR", "GS", "GT",
    "GU", "GW", "GY", "HK", "HM", "HN", "HR", "HT", "HU", "ID", "IE", "IL", "IM",
    "IN", "IO", "IQ", "IR", "IS", "IT", "JE", "JM", "JO", "JP", "KE", "KG", "KH",
    "KI", "KM", "KN", "KP", "KR", "KW", "KY", "KZ", "LA", "LB", "LC", "LI", "LK",
    "LR", "LS", "LT", "LU", "LV", "LY", "MA", "MC", "MD", "ME", "MF", "MG", "MH",
    "MK", "ML", "MM", "MN", "MO", "MP", "MQ", "MR", "MS", "MT", "MU", "MV", "MW",
    "MX", "MY", "MZ", "NA", "NC", "NE", "NF", "NG", "NI", "NL", "NO", "NP", "NR",
    "NU", "NZ", "OM", "PA", "PE", "PF", "PG", "PH", "PK", "PL", "PM", "PN", "PR",
    "PS", "PT", "PW", "PY", "QA", "RE", "RO", "RS", "RU", "RW", "SA", "SB", "SC",
    "SD", "SE", "SG", "SH", "SI", "SJ", "SK", "SL", "SM", "SN", "SO", "SR", "SS",
    "ST", "SV", "SX", "SY", "SZ", "TC", "TD", "TF", "TG", "TH", "TJ", "TK", "TL",
    "TM", "TN", "TO", "TR", "TT", "TV", "TW", "TZ", "UA", "UG", "UM", "US", "UY",
    "UZ", "VA", "VC", "VE", "VG", "VI", "VN", "VU", "WF", "WS", "YE", "YT", "ZA",
    "ZM", "ZW"
))
# fmt: on

#: Codes people reach for that ISO does not assign, and what to use instead.
COUNTRY_ALIASES = {"UK": "GB", "EL": "GR", "EN": "GB"}

#: Passed as validation context when a model is read back out of storage.
#:
#: Tightening a validator must never strand a row that an earlier release
#: accepted. It did once: adding the assigned-country-code check made a stored
#: ``UK`` unreadable, and because every repository read revalidates, the record
#: could no longer be listed, patched, *or deleted* — the operator was left with
#: no way to correct it short of editing SQLite by hand.
#:
#: So input is strict and storage is lenient. A validator that rejects a value
#: purely because an operator would not have meant it must relax under this
#: context; one that guards a downstream injection or a privilege boundary must
#: not, because those apply to the value regardless of how it arrived.
#: Re-saving still goes through strict validation, so a loaded-but-invalid
#: record can be fixed or removed and never silently rewritten.
STORED = {"stored": True}


def is_stored(info: Any) -> bool:
    """True when a validator is running against a record read from storage."""
    return bool(getattr(info, "context", None) and info.context.get("stored"))


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
    # sent, which is the same silent-no-op failure the assigned-country-code
    # check above exists to prevent.
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
