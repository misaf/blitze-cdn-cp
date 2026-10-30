"""Effective virtual hosts, derived from a zone, its rules, and its records."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.core.domain.site_policy import SitePolicy
from blitzecdn.core.domain.validation import HOST_NAME, hostname


class CdnSite(BaseModel):
    """Validated desired state for one CDN virtual host.

    Its ``name`` already encodes the zone and rule it came from, via
    :func:`~blitzecdn.capabilities.dns.domain.hosts.host_name`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    policy: SitePolicy = Field(default_factory=SitePolicy)
    #: The hostnames this host answers on. Empty is legal and renders no server
    #: block.
    server_names: tuple[str, ...] = Field(default=(), max_length=100)
    origin_host: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not HOST_NAME.fullmatch(normalized):
            raise ValueError(
                "name must contain only a-z, 0-9, and hyphens, and start with "
                "a letter or a digit"
            )
        return normalized

    @field_validator("server_names")
    @classmethod
    def validate_server_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            dict.fromkeys(hostname(item, wildcard=True) for item in values)
        )
        if len(normalized) != len(values):
            raise ValueError("server_names must be unique")
        return normalized

    @field_validator("origin_host")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        return hostname(value)

    @model_validator(mode="after")
    def validate_contributed_site_checks(self) -> Self:
        """The rules a contract states about a host rather than about a zone."""
        self.policy.validate_for_site(self.name)
        return self

    @property
    def serves_traffic(self) -> bool:
        """Whether this site should contribute a server block at all.

        With no hostnames, an empty ``server_name`` would make it the listener's
        default server. ``enabled`` stays a separate switch.
        """
        return bool(self.server_names)
