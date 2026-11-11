"""The error_pages capability's contract: what visitors see when a site fails.

Only presentation. Nothing here checks an origin's health or fails over: an
error page replaces the body of an error the edge already answers, and
maintenance mode answers every request itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from pydantic import ConfigDict, Field, field_validator

from blitzecdn.core.domain.policy import CapabilityPolicy
from blitzecdn.core.domain.values import HTML_LIMIT, HtmlDocument, NginxValue

#: The statuses a site may brand. The 5xx are what an origin, or the edge on
#: its behalf, answers when the origin fails; 403 and 429 are what the edge's
#: own rules (the firewall and the rate limit) answer.
ERROR_PAGE_STATUSES = ("403", "429", "500", "502", "503", "504")

#: The statuses an origin's own response is replaced for. An origin's 403 or
#: 429 is its answer to that request and reaches the visitor as it was sent.
ORIGIN_ERROR_STATUSES = ("500", "502", "503", "504")

#: Bytes one page may take, UTF-8 encoded. A page ships inside every
#: configuration release and is served from the edge's own disk, so it is
#: meant to be self-contained: inline styles and images, nothing fetched from
#: an origin that may be the thing that is down.
PAGE_LIMIT = HTML_LIMIT

#: The longest Retry-After maintenance mode may advertise: one week.
RETRY_AFTER_LIMIT = 7 * 24 * 3600


def _error_status(value: str) -> str:
    candidate = str(value).strip()
    if candidate not in ERROR_PAGE_STATUSES:
        raise ValueError(
            f"{value!r} is not a status with an error page; choose one "
            "of " + ", ".join(ERROR_PAGE_STATUSES)
        )
    return candidate


#: One of :data:`ERROR_PAGE_STATUSES`.
ErrorStatus = Annotated[str, NginxValue("status", _error_status)]


class ErrorPagesPolicy(CapabilityPolicy):
    """Error presentation and maintenance mode requested by one site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error_pages: dict[ErrorStatus, HtmlDocument] = Field(
        default_factory=dict,
        description=(
            "HTML served in place of an error, by status: "
            + ", ".join(ERROR_PAGE_STATUSES)
            + ". A 5xx page replaces the origin's own 5xx as well as the "
            "edge's when the origin cannot be reached; 403 and 429 replace "
            "only the edge's firewall and rate-limit answers. With "
            "under_attack_mode, an origin's own 5xx passes through and only the "
            "edge's 502 and 504 are replaced. The status is "
            f"kept. At most {PAGE_LIMIT // 1024} KiB each, and self-contained: "
            "the origin may be what is down."
        ),
    )
    maintenance_mode: bool = Field(
        default=False,
        description=(
            "Answer every request with the maintenance page, status 503 and "
            "Retry-After, without contacting the origin. firewall.allow_sources "
            "are exempt and reach the origin as usual."
        ),
    )
    maintenance_page: HtmlDocument | None = Field(
        default=None,
        description=(
            "HTML served in maintenance mode. Unset uses error_pages' 503 "
            f"page, else a plain built-in one. At most {PAGE_LIMIT // 1024} KiB."
        ),
    )
    maintenance_retry_after: int = Field(
        default=300,
        ge=1,
        le=RETRY_AFTER_LIMIT,
        description="Seconds sent as Retry-After in maintenance mode.",
    )

    @field_validator("error_pages", mode="before")
    @classmethod
    def refuse_repeated_statuses(cls, pages: object) -> object:
        """``403`` and ``" 403"`` would otherwise collapse silently."""
        if not isinstance(pages, Mapping):
            return pages
        seen: set[str] = set()
        for status in pages:
            key = str(status).strip()
            if key in seen:
                raise ValueError(f"status {key} is listed twice")
            seen.add(key)
        return pages

    @field_validator("error_pages")
    @classmethod
    def sort_statuses(cls, pages: dict[str, str]) -> dict[str, str]:
        return {status: pages[status] for status in sorted(pages)}

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        requested = tuple(
            name
            for name, asked in (
                ("error_pages", bool(self.error_pages)),
                ("maintenance_mode", self.maintenance_mode),
            )
            if asked
        )
        return {"error_pages": requested} if requested else {}
