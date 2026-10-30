"""The security capability's contract: firewall rules and Under Attack Mode.

The country and method tables have one consumer, so they live here.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from typing import Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from blitzecdn.core.domain.policy import CapabilityPolicy
from blitzecdn.core.domain.validation import OmittedWhenEmpty, unique

COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")

HTTP_METHOD = re.compile(r"^[A-Z][A-Z-]{1,19}$")

#: ISO 3166-1 alpha-2 as MaxMind emits it, checked against the assigned list:
#: `UK` would validate by shape and then match nobody, because the code is GB.
ISO_3166_1_ALPHA_2 = frozenset(
    (
        "AD",
        "AE",
        "AF",
        "AG",
        "AI",
        "AL",
        "AM",
        "AO",
        "AQ",
        "AR",
        "AS",
        "AT",
        "AU",
        "AW",
        "AX",
        "AZ",
        "BA",
        "BB",
        "BD",
        "BE",
        "BF",
        "BG",
        "BH",
        "BI",
        "BJ",
        "BL",
        "BM",
        "BN",
        "BO",
        "BQ",
        "BR",
        "BS",
        "BT",
        "BV",
        "BW",
        "BY",
        "BZ",
        "CA",
        "CC",
        "CD",
        "CF",
        "CG",
        "CH",
        "CI",
        "CK",
        "CL",
        "CM",
        "CN",
        "CO",
        "CR",
        "CU",
        "CV",
        "CW",
        "CX",
        "CY",
        "CZ",
        "DE",
        "DJ",
        "DK",
        "DM",
        "DO",
        "DZ",
        "EC",
        "EE",
        "EG",
        "EH",
        "ER",
        "ES",
        "ET",
        "FI",
        "FJ",
        "FK",
        "FM",
        "FO",
        "FR",
        "GA",
        "GB",
        "GD",
        "GE",
        "GF",
        "GG",
        "GH",
        "GI",
        "GL",
        "GM",
        "GN",
        "GP",
        "GQ",
        "GR",
        "GS",
        "GT",
        "GU",
        "GW",
        "GY",
        "HK",
        "HM",
        "HN",
        "HR",
        "HT",
        "HU",
        "ID",
        "IE",
        "IL",
        "IM",
        "IN",
        "IO",
        "IQ",
        "IR",
        "IS",
        "IT",
        "JE",
        "JM",
        "JO",
        "JP",
        "KE",
        "KG",
        "KH",
        "KI",
        "KM",
        "KN",
        "KP",
        "KR",
        "KW",
        "KY",
        "KZ",
        "LA",
        "LB",
        "LC",
        "LI",
        "LK",
        "LR",
        "LS",
        "LT",
        "LU",
        "LV",
        "LY",
        "MA",
        "MC",
        "MD",
        "ME",
        "MF",
        "MG",
        "MH",
        "MK",
        "ML",
        "MM",
        "MN",
        "MO",
        "MP",
        "MQ",
        "MR",
        "MS",
        "MT",
        "MU",
        "MV",
        "MW",
        "MX",
        "MY",
        "MZ",
        "NA",
        "NC",
        "NE",
        "NF",
        "NG",
        "NI",
        "NL",
        "NO",
        "NP",
        "NR",
        "NU",
        "NZ",
        "OM",
        "PA",
        "PE",
        "PF",
        "PG",
        "PH",
        "PK",
        "PL",
        "PM",
        "PN",
        "PR",
        "PS",
        "PT",
        "PW",
        "PY",
        "QA",
        "RE",
        "RO",
        "RS",
        "RU",
        "RW",
        "SA",
        "SB",
        "SC",
        "SD",
        "SE",
        "SG",
        "SH",
        "SI",
        "SJ",
        "SK",
        "SL",
        "SM",
        "SN",
        "SO",
        "SR",
        "SS",
        "ST",
        "SV",
        "SX",
        "SY",
        "SZ",
        "TC",
        "TD",
        "TF",
        "TG",
        "TH",
        "TJ",
        "TK",
        "TL",
        "TM",
        "TN",
        "TO",
        "TR",
        "TT",
        "TV",
        "TW",
        "TZ",
        "UA",
        "UG",
        "UM",
        "US",
        "UY",
        "UZ",
        "VA",
        "VC",
        "VE",
        "VG",
        "VI",
        "VN",
        "VU",
        "WF",
        "WS",
        "YE",
        "YT",
        "ZA",
        "ZM",
        "ZW",
    )
)
# fmt: on

#: Codes people reach for that ISO does not assign, and what to use instead.
COUNTRY_ALIASES = {"UK": "GB", "EL": "GR", "EN": "GB"}


class SiteFirewall(OmittedWhenEmpty):
    """Per-hostname request filtering applied by Nginx; omitted when empty."""

    allow_sources: tuple[str, ...] = Field(
        default=(),
        max_length=200,
        description=(
            "CIDR networks exempted from deny_sources, matched first. Not an "
            "allowlist on its own: without a deny entry covering everything "
            "else, the site stays open."
        ),
    )
    deny_sources: tuple[str, ...] = Field(
        default=(), max_length=200, description="CIDR networks answered with 403."
    )
    allowed_countries: tuple[str, ...] = Field(
        default=(),
        max_length=250,
        description=(
            "ISO 3166-1 alpha-2 codes; every other country gets a 403. Needs the "
            "geoip capability."
        ),
    )
    denied_countries: tuple[str, ...] = Field(
        default=(),
        max_length=250,
        description="ISO 3166-1 alpha-2 codes answered with 403. Needs geoip.",
    )
    denied_methods: tuple[str, ...] = Field(
        default=(), max_length=20, description="HTTP methods answered with 405."
    )
    denied_paths: tuple[str, ...] = Field(
        default=(), max_length=100, description="URI prefixes answered with 403."
    )

    def replacing(self, lists: Mapping[str, Sequence[str]]) -> SiteFirewall:
        """This firewall with the named lists replaced and the rest kept.

        Per list, as an operator editing one rule means; ``PATCH`` replaces the whole
        block, deliberately. Revalidated, since every list reaches an nginx directive.
        """
        unknown = sorted(set(lists) - set(type(self).model_fields))
        if unknown:
            raise ValueError("a firewall has no such rule list: " + ", ".join(unknown))
        return type(self).model_validate(self.model_dump() | dict(lists))

    @field_validator("allow_sources", "deny_sources")
    @classmethod
    def validate_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = []
        for item in values:
            candidate = item.strip()
            try:
                normalized.append(str(ipaddress.ip_network(candidate, strict=True)))
            except ValueError as error:
                raise ValueError(
                    f"{item!r} is not an IP address or CIDR network: {error}"
                ) from error
        return unique(tuple(normalized), "source")

    @field_validator("allowed_countries", "denied_countries")
    @classmethod
    def validate_countries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = []
        for item in values:
            candidate = item.strip().upper()
            if not COUNTRY_CODE.fullmatch(candidate):
                raise ValueError(
                    f"{item!r} is not an ISO 3166-1 alpha-2 country code such as 'DE'"
                )
            if candidate in COUNTRY_ALIASES:
                raise ValueError(
                    f"{item!r} is not an ISO 3166-1 alpha-2 country code; "
                    f"use {COUNTRY_ALIASES[candidate]!r}"
                )
            if candidate not in ISO_3166_1_ALPHA_2:
                raise ValueError(
                    f"{item!r} is not an ISO 3166-1 alpha-2 country code such as 'DE'"
                )
            normalized.append(candidate)
        return unique(tuple(normalized), "country")

    @field_validator("denied_methods")
    @classmethod
    def validate_methods(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = []
        for item in values:
            candidate = item.strip().upper()
            if not HTTP_METHOD.fullmatch(candidate):
                raise ValueError(f"{item!r} is not an HTTP method such as 'DELETE'")
            normalized.append(candidate)
        return unique(tuple(normalized), "method")

    @field_validator("denied_paths")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = []
        for item in values:
            candidate = item.strip()
            if not candidate.startswith("/"):
                raise ValueError(f"denied path {item!r} must start with '/'")
            if len(candidate) > 512:
                raise ValueError("denied paths must be at most 512 characters")
            if any(character in candidate for character in " \t\r\n;{}\"'\\"):
                raise ValueError(
                    f"denied path {item!r} may not contain whitespace, quotes, "
                    "backslashes, ';', '{' or '}'"
                )
            normalized.append(candidate)
        return unique(tuple(normalized), "path")

    @model_validator(mode="after")
    def validate_country_rules(self) -> Self:
        overlap = set(self.allowed_countries) & set(self.denied_countries)
        if overlap:
            raise ValueError(
                "allowed_countries and denied_countries both list "
                + ", ".join(sorted(overlap))
            )
        return self

    @property
    def requires_geoip(self) -> bool:
        return bool(self.allowed_countries or self.denied_countries)


class SecurityPolicy(CapabilityPolicy):
    """Request filtering requested by one site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    under_attack_mode: bool = Field(
        default=False,
        description=(
            "Require a signed BlitzeCDN browser clearance before cache or origin "
            "processing."
        ),
    )
    # The block is replaced wholesale when patched.
    firewall: SiteFirewall = Field(
        default=SiteFirewall(),
        description=(
            "Per-hostname request filtering. Empty serves everyone. Never applied "
            "to /.well-known/acme-challenge/, so no rule can block renewal."
        ),
    )

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """The capabilities this policy requests.

        Country rules need both ``security`` and ``geoip``, named through their block
        path (``firewall.allowed_countries``), the path a patch sets.
        """
        requested: dict[str, tuple[str, ...]] = {}
        security = tuple(
            name
            for name, asked in (
                ("under_attack_mode", self.under_attack_mode),
                ("firewall", not self.firewall.empty),
            )
            if asked
        )
        if security:
            requested["security"] = security
        countries = tuple(
            name
            for name, asked in (
                ("firewall.allowed_countries", bool(self.firewall.allowed_countries)),
                ("firewall.denied_countries", bool(self.firewall.denied_countries)),
            )
            if asked
        )
        if countries:
            requested["geoip"] = countries
        return requested
