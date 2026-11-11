"""Effective virtual hosts, derived from a zone, its rules, and its records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.core.domain.paths import PathLocation
from blitzecdn.core.domain.site_policy import SitePolicy
from blitzecdn.core.domain.validation import refuse_internal_origin
from blitzecdn.core.domain.values import Hostname, VirtualHostName, WildcardHostname


class CdnSite(BaseModel):
    """Validated desired state for one CDN virtual host.

    Its ``name`` already encodes the zone and rule it came from, via
    :func:`~blitzecdn.capabilities.sites.domain.hosts.host_name`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: VirtualHostName
    policy: SitePolicy = Field(default_factory=SitePolicy)
    #: The hostnames this host answers on. Empty is legal and renders no server
    #: block.
    server_names: tuple[WildcardHostname, ...] = Field(default=(), max_length=100)
    origin_host: Hostname
    #: The path rules this host serves, in the order they are matched: the
    #: first whose path covers a request answers it, and a request no location
    #: covers is served by the host's own policy.
    locations: tuple[PathLocation, ...] = ()

    @field_validator("server_names")
    @classmethod
    def validate_server_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("server_names must be unique")
        return values

    @field_validator("origin_host")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        refuse_internal_origin(value)
        return value

    @model_validator(mode="after")
    def validate_locations(self) -> Self:
        """Each location names this host's hostnames, and each rule appears once."""
        rules = [location.rule for location in self.locations]
        if len(set(rules)) != len(rules):
            raise ValueError("a path rule may appear once among a host's locations")
        scoped = SitePolicy.path_setting_names()
        for location in self.locations:
            stray = sorted(set(location.server_names) - set(self.server_names))
            if stray:
                raise ValueError(
                    f"location {location.rule!r} names hostnames this host does "
                    "not serve: " + ", ".join(stray)
                )
            hostname_only = sorted(set(location.settings) - scoped)
            if hostname_only:
                raise ValueError(
                    f"location {location.rule!r} sets settings decided per "
                    "hostname: " + ", ".join(hostname_only)
                )
        return self

    @model_validator(mode="after")
    def validate_contributed_site_checks(self) -> Self:
        """The rules a contract states about a host rather than about a zone."""
        self.policy.validate_for_site(self)
        return self

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """What the host's policy and each path location's policy require.

        A location can turn on a path-scoped setting its host leaves off, such
        as the BZ-IPCountry header, so it can need a capability the host does
        not. Its settings are named with the path rule that set them.
        """
        requested = {
            token: list(settings)
            for token, settings in self.policy.capability_requirements.items()
        }
        host = self.policy.settings(mode="json")
        for location in self.locations:
            changed = {
                name
                for name, value in location.settings.items()
                if host.get(name) != value
            }
            if not changed:
                continue
            scoped = self.policy.applying(location.settings).capability_requirements
            for token, settings in scoped.items():
                requested.setdefault(token, []).extend(
                    f"{setting} (path rule {location.rule!r})"
                    for setting in settings
                    # Nested settings are named `block.field`.
                    if setting.split(".", 1)[0] in changed
                )
        return {
            token: tuple(dict.fromkeys(settings))
            for token, settings in requested.items()
            if settings
        }

    @property
    def serves_traffic(self) -> bool:
        """Whether this site should contribute a server block at all.

        With no hostnames, an empty ``server_name`` would make it the listener's
        default server. ``enabled`` stays a separate switch.
        """
        return bool(self.server_names)
