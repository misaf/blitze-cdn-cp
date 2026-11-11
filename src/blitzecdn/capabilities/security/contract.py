"""The security capability's contract: firewall rules and Under Attack Mode.

The country and method tables have one consumer, so they live here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Self

from pydantic import (
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from blitzecdn.core.domain.policy import CapabilityPolicy
from blitzecdn.core.domain.validation import OmittedWhenEmpty, unique
from blitzecdn.core.domain.values import IpNetwork, NginxValue, UrlPath, regex

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

#: Nginx refuses a configuration token longer than its 4 KiB read buffer, and
#: the allowed paths render as one quoted regex. Bytes, not characters: a
#: non-ASCII path takes up to four bytes per character in that buffer.
ALLOWED_PATHS_PATTERN_LIMIT = 3500

#: Codes people reach for that ISO does not assign, and what to use instead.
COUNTRY_ALIASES = {"UK": "GB", "EL": "GR", "EN": "GB"}


def _country_code(value: str) -> str:
    candidate = value.strip().upper()
    if not COUNTRY_CODE.fullmatch(candidate):
        raise ValueError(
            f"{value!r} is not an ISO 3166-1 alpha-2 country code such as 'DE'"
        )
    if candidate in COUNTRY_ALIASES:
        raise ValueError(
            f"{value!r} is not an ISO 3166-1 alpha-2 country code; "
            f"use {COUNTRY_ALIASES[candidate]!r}"
        )
    if candidate not in ISO_3166_1_ALPHA_2:
        raise ValueError(
            f"{value!r} is not an ISO 3166-1 alpha-2 country code such as 'DE'"
        )
    return candidate


def _http_method(value: str) -> str:
    candidate = value.strip().upper()
    if not HTTP_METHOD.fullmatch(candidate):
        raise ValueError(f"{value!r} is not an HTTP method such as 'DELETE'")
    return candidate


#: An assigned ISO 3166-1 alpha-2 code, upper case.
CountryCode = Annotated[str, NginxValue("country code", _country_code)]

#: An HTTP method name, upper case.
HttpMethod = Annotated[str, NginxValue("HTTP method", _http_method)]


class SiteFirewall(OmittedWhenEmpty):
    """Per-hostname request filtering applied by Nginx; omitted when empty."""

    allow_sources: tuple[IpNetwork, ...] = Field(
        default=(),
        max_length=200,
        description=(
            "CIDR networks exempted from deny_sources and from the country and "
            "method rules (allowed_countries, denied_countries, "
            "allowed_methods, denied_methods). Path rules still apply to them. "
            "Not an allowlist on its own: without a deny entry covering "
            "everything else, the site stays open."
        ),
    )
    deny_sources: tuple[IpNetwork, ...] = Field(
        default=(), max_length=200, description="CIDR networks answered with 403."
    )
    allowed_countries: tuple[CountryCode, ...] = Field(
        default=(),
        max_length=250,
        description=(
            "ISO 3166-1 alpha-2 codes; every other country gets a 403. Needs the "
            "geoip capability."
        ),
    )
    denied_countries: tuple[CountryCode, ...] = Field(
        default=(),
        max_length=250,
        description="ISO 3166-1 alpha-2 codes answered with 403. Needs geoip.",
    )
    allowed_methods: tuple[HttpMethod, ...] = Field(
        default=(),
        max_length=20,
        description=(
            "HTTP methods served; every other method gets a 405. HEAD is added "
            "when GET is listed. Allowing only GET also refuses CORS preflight "
            "(OPTIONS) and form posts. Cannot be combined with denied_methods."
        ),
    )
    denied_methods: tuple[HttpMethod, ...] = Field(
        default=(), max_length=20, description="HTTP methods answered with 405."
    )
    allowed_paths: tuple[UrlPath, ...] = Field(
        default=(),
        max_length=100,
        description=(
            "Paths served; every other path gets a 403. An entry ending in '/' "
            "admits everything under it, any other entry only that exact path. "
            "Matched against the decoded, normalized URI. denied_paths still "
            "apply inside an allowed prefix."
        ),
    )
    denied_paths: tuple[UrlPath, ...] = Field(
        default=(), max_length=100, description="URI prefixes answered with 403."
    )
    case_insensitive_paths: bool = Field(
        default=False,
        description=(
            "Match allowed_paths and denied_paths regardless of ASCII letter "
            "case, so a denied /admin also refuses /Admin and /ADMIN/login. "
            "Off by default: paths match exactly as written. Turn it on when "
            "the origin treats paths case-insensitively (IIS, ASP.NET, many "
            "CMSs), or a different spelling reaches the denied path there."
        ),
    )

    @property
    def empty(self) -> bool:
        """No rule lists: the matching flag alone filters nothing."""
        return not any(
            getattr(self, field)
            for field in type(self).model_fields
            if field != "case_insensitive_paths"
        )

    @property
    def rule_count(self) -> int:
        return sum(
            len(getattr(self, field))
            for field in type(self).model_fields
            if field != "case_insensitive_paths"
        )

    def replacing(self, lists: Mapping[str, object]) -> SiteFirewall:
        """This firewall with the named lists (or flag) replaced and the rest kept.

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
        return unique(values, "source")

    @field_validator("allowed_countries", "denied_countries")
    @classmethod
    def validate_countries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return unique(values, "country")

    @field_validator("allowed_methods", "denied_methods")
    @classmethod
    def validate_methods(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return unique(values, "method")

    @field_validator("allowed_methods")
    @classmethod
    def head_follows_get(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Nginx answers HEAD from the GET handler; refusing it breaks clients."""
        if "GET" in values and "HEAD" not in values:
            return (*values, "HEAD")
        return values

    @field_validator("allowed_paths", "denied_paths")
    @classmethod
    def validate_paths(
        cls, values: tuple[str, ...], info: ValidationInfo
    ) -> tuple[str, ...]:
        normalized = unique(values, "path")
        if info.field_name == "denied_paths" and "/" in normalized:
            # `location ^~ /` duplicates the site's own `location /`, which
            # fails the edge's configuration test.
            raise ValueError(
                "denied path '/' would block the whole site; list the paths "
                "to serve in allowed_paths instead"
            )
        return normalized

    @model_validator(mode="after")
    def validate_country_rules(self) -> Self:
        overlap = set(self.allowed_countries) & set(self.denied_countries)
        if overlap:
            raise ValueError(
                "allowed_countries and denied_countries both list "
                + ", ".join(sorted(overlap))
            )
        pattern = allowed_paths_pattern(self.allowed_paths).encode("utf-8")
        if len(pattern) > ALLOWED_PATHS_PATTERN_LIMIT:
            raise ValueError(
                "allowed_paths are too long together for one Nginx rule; list "
                "shared prefixes ending in '/' instead of individual paths"
            )
        if self.allowed_methods and self.denied_methods:
            raise ValueError(
                "allowed_methods and denied_methods cannot both be set: an "
                "allowlist already refuses every method it does not name"
            )
        return self

    @property
    def requires_geoip(self) -> bool:
        return bool(self.allowed_countries or self.denied_countries)


def allowed_paths_pattern(paths: Sequence[str]) -> str:
    """The alternation the edge renders: a prefix for '/'-terminated paths.

    Mirrors ``security-access.conf.j2``, whose ``regex_escape`` is :func:`regex`.
    """
    return "|".join(regex(path) + ("" if path.endswith("/") else "$") for path in paths)


#: The request rates a site may choose, slowest first. A ladder rather than any
#: number because Nginx fixes the rate per shared-memory zone: every site on a
#: rung shares that rung's zone, keyed per site and client, so the edge declares
#: at most one zone per rung however many sites limit their traffic.
RATE_LIMIT_RATES = (
    "10r/m",
    "30r/m",
    "1r/s",
    "2r/s",
    "5r/s",
    "10r/s",
    "20r/s",
    "50r/s",
    "100r/s",
    "200r/s",
    "500r/s",
    "1000r/s",
)

#: Statuses `limit_req_status` may answer with that a client reads as "slow down".
RATE_LIMIT_STATUSES = (429, 503)


def _rate(value: str) -> str:
    candidate = value.strip().replace(" ", "")
    if candidate not in RATE_LIMIT_RATES:
        raise ValueError(
            f"{value!r} is not an available rate; choose one of "
            + ", ".join(RATE_LIMIT_RATES)
        )
    return candidate


#: One rung of :data:`RATE_LIMIT_RATES`.
RateLimitRate = Annotated[str, NginxValue("rate", _rate)]


def rate_limit_zone(rate: str) -> str:
    """The shared zone one rung renders as; mirrors ``security-http.conf.j2``."""
    return "blitzecdn_rate_" + rate.replace("/", "")


class SiteRateLimit(OmittedWhenEmpty):
    """A per-client request rate for one site; omitted when no rate is set."""

    rate: RateLimitRate | None = Field(
        default=None,
        description=(
            "Requests each client address may make, as one of "
            + ", ".join(RATE_LIMIT_RATES)
            + ". Requests over it are answered with status. Unset serves "
            "everyone unlimited."
        ),
    )
    burst: int = Field(
        default=0,
        ge=0,
        le=10000,
        description=(
            "Requests a client may make above the rate before it is refused. "
            "They are served at once, not queued."
        ),
    )
    status: int = Field(
        default=429,
        description="The status refused requests get: 429 or 503.",
    )
    paths: tuple[UrlPath, ...] = Field(
        default=(),
        max_length=100,
        description=(
            "Paths the limit counts; empty counts every request. An entry ending "
            "in '/' covers everything under it, any other entry only that exact "
            "path; firewall.case_insensitive_paths applies to them too. "
            "firewall.allow_sources are never limited."
        ),
    )

    @property
    def empty(self) -> bool:
        return self.rate is None

    @property
    def zone(self) -> str | None:
        return None if self.rate is None else rate_limit_zone(self.rate)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: int) -> int:
        if value not in RATE_LIMIT_STATUSES:
            raise ValueError("a rate limit answers with 429 or 503")
        return value

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return unique(values, "path")

    @model_validator(mode="after")
    def validate_rate_is_set(self) -> Self:
        if self.rate is None and (self.burst or self.status != 429 or self.paths):
            raise ValueError("burst, status and paths need a rate to apply to")
        pattern = allowed_paths_pattern(self.paths).encode("utf-8")
        if len(pattern) > ALLOWED_PATHS_PATTERN_LIMIT:
            raise ValueError(
                "rate-limited paths are too long together for one Nginx rule; "
                "list shared prefixes ending in '/' instead of individual paths"
            )
        return self


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

    # The block is replaced wholesale when patched.
    rate_limit: SiteRateLimit = Field(
        default=SiteRateLimit(),
        description=(
            "Per-client request rate for the site. Empty serves everyone "
            "unlimited. Never applied to /.well-known/acme-challenge/."
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
                ("rate_limit", not self.rate_limit.empty),
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
