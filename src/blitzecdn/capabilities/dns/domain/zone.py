"""The delegated zone: the name a customer has pointed at our nameservers.

Its own module because it is its own aggregate. A zone holds no records — they
are stored separately and keyed by zone, so a zone with a thousand records is
not rewritten to change one of them — and its only invariant is that the name
is delegable, which is a different question from anything
:mod:`~blitzecdn.capabilities.dns.domain.record` asks.
"""

from __future__ import annotations

import ipaddress

from pydantic import BaseModel, ConfigDict, field_validator

from blitzecdn.core.domain.validation import hostname

__all__ = ["Domain"]


class Domain(BaseModel):
    """A DNS zone a customer has delegated to us.

    Holds no records itself — they are stored separately and keyed by domain,
    so a zone with a thousand records is not rewritten to change one of them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = hostname(value)
        try:
            ipaddress.ip_address(normalized)
        except ValueError:
            if "." not in normalized:
                raise ValueError(
                    "domain must be a delegable zone such as 'example.com'"
                ) from None
            return normalized
        raise ValueError("domain must be a name, not an IP address")
