"""Per-site request filtering and emergency security policy."""

from __future__ import annotations

import ipaddress
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.core.validation import (
    COUNTRY_ALIASES,
    COUNTRY_CODE,
    HTTP_METHOD,
    ISO_3166_1_ALPHA_2,
    unique,
)


class SiteFirewall(BaseModel):
    """Per-hostname request filtering applied by Nginx at the edge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allow_sources: tuple[str, ...] = Field(default=(), max_length=200)
    deny_sources: tuple[str, ...] = Field(default=(), max_length=200)
    allowed_countries: tuple[str, ...] = Field(default=(), max_length=250)
    denied_countries: tuple[str, ...] = Field(default=(), max_length=250)
    denied_methods: tuple[str, ...] = Field(default=(), max_length=20)
    denied_paths: tuple[str, ...] = Field(default=(), max_length=100)

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
    def empty(self) -> bool:
        return not any(getattr(self, field) for field in SiteFirewall.model_fields)

    @property
    def requires_geoip(self) -> bool:
        return bool(self.allowed_countries or self.denied_countries)


class SecurityPolicy(BaseModel):
    """Request filtering requested by one site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    under_attack_mode: bool = False
    # The block is replaced wholesale when patched.
    firewall: SiteFirewall = SiteFirewall()
