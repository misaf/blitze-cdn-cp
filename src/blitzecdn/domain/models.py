from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_DNS_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_SITE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_DURATION = re.compile(r"^(?:0|[1-9]\d*)(?:ms|[smhdw])$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_HTTP_METHOD = re.compile(r"^[A-Z][A-Z-]{1,19}$")

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


#: Version of the desired-state document handed to Ansible.
#:
#: This is the contract between the control plane and the edge roles, which
#: ship and version independently. Bump it whenever the shape of
#: ``blitzecdn_nginx_sites`` changes in a way an older role cannot honour —
#: a new required key, a renamed key, or new semantics for an existing one.
#: Adding a key that older roles can safely ignore does not need a bump.
#: The nginx role refuses to run against a version it does not support, so
#: skew fails before a host is touched instead of midway through a rollout.
#:
#: 2 added the per-site ``firewall`` block. Role argument validation rejects a
#: suboption it does not declare, so a 1.6.0 role cannot ignore the new key
#: even on a site that leaves the block empty — hence the bump rather than an
#: additive change.
DESIRED_STATE_VERSION = 2

#: Certificates BlitzeCDN installs itself live here, one directory per site.
MANAGED_TLS_ROOT = "/etc/blitzecdn/tls"

#: Deploying a site makes Ansible write these paths on every edge as root, so an
#: operator must not be able to aim them at arbitrary files such as /etc/cron.d.
_CERTIFICATE_ROOTS = (f"{MANAGED_TLS_ROOT}/", "/etc/ssl/", "/etc/letsencrypt/")


def managed_certificate_paths(site_name: str) -> tuple[str, str]:
    """Return the chain and key paths BlitzeCDN manages for ``site_name``."""
    return (
        f"{MANAGED_TLS_ROOT}/{site_name}/fullchain.pem",
        f"{MANAGED_TLS_ROOT}/{site_name}/privkey.pem",
    )


class OriginScheme(StrEnum):
    HTTP = "http"
    HTTPS = "https"


class CertificateMode(StrEnum):
    DISABLED = "disabled"
    EXISTING = "existing"
    UPLOADED = "uploaded"
    REQUESTED = "requested"


class CertificateSource(StrEnum):
    UPLOADED = "uploaded"
    ACME = "acme"


class DeploymentStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    ABANDONED = "abandoned"


def _hostname(value: str, *, wildcard: bool = False) -> str:
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
        if prefix:
            raise ValueError("wildcards cannot be used with IP addresses")
        return candidate
    except ValueError as ip_error:
        if all(_DNS_LABEL.fullmatch(label) for label in candidate.split(".")):
            return prefix + candidate
        raise ValueError(f"invalid DNS hostname: {value!r}") from ip_error


class SiteFirewall(BaseModel):
    """Per-hostname request filtering applied at the edge.

    This has to live in nginx rather than in ``blitzecdn_firewall``. A packet
    filter sees an address, a port and a protocol; it does not see the ``Host``
    header, and on an edge every managed site shares one address and one pair
    of ports. Filtering per hostname is only possible once the request has been
    parsed, which means the virtual host.

    **The posture is open.** An empty block behaves exactly as no block at all,
    and the rules below subtract from a site that otherwise serves everyone.
    ``allow_sources`` is not a whitelist — nginx's access module takes the first
    matching rule, and the template emits allows before denies, so an allow
    entry is an *exemption* from the deny entries under it. To close a site,
    deny ``0.0.0.0/0`` and ``::/0`` and list the exemptions in
    ``allow_sources``; there is no implicit trailing deny to depend on.

    The rules are evaluated by two different nginx phases and do not compose in
    the order they are written: ``denied_countries`` / ``allowed_countries`` and
    ``denied_methods`` compile to ``return`` in the rewrite phase, which runs
    before the access phase that ``allow`` / ``deny`` belong to. A country
    denial therefore wins over a source exemption. This is the safer of the two
    orderings, but it does mean ``allow_sources`` cannot be used to let an
    address bypass a country block.

    None of it applies to ``/.well-known/acme-challenge/``. Certificate
    issuance depends on that path being reachable from whichever address the CA
    validates from, and a site that locked itself out of renewal would fail
    weeks later, at expiry, with nothing connecting the outage to the rule that
    caused it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    allow_sources: tuple[str, ...] = Field(default=(), max_length=200)
    deny_sources: tuple[str, ...] = Field(default=(), max_length=200)
    #: ISO 3166-1 alpha-2, resolved from the client address by ngx_http_geoip2.
    #: Requires ``blitzecdn_nginx_geoip_enabled`` on the edge; the role refuses
    #: to converge a site that asks for country rules without it rather than
    #: rendering a config that silently admits everyone.
    allowed_countries: tuple[str, ...] = Field(default=(), max_length=250)
    denied_countries: tuple[str, ...] = Field(default=(), max_length=250)
    denied_methods: tuple[str, ...] = Field(default=(), max_length=20)
    #: URI prefixes answered with 403 before the request reaches the origin.
    denied_paths: tuple[str, ...] = Field(default=(), max_length=100)

    @field_validator("allow_sources", "deny_sources")
    @classmethod
    def validate_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Normalise to CIDR, rejecting a network with host bits set.

        ``strict=True`` is deliberate: ``ipaddress`` would happily read
        ``203.0.113.4/24`` as ``203.0.113.0/24``, turning a rule an operator
        wrote for one address into one covering 256 of them.
        """
        normalized = []
        for item in values:
            candidate = item.strip()
            try:
                normalized.append(str(ipaddress.ip_network(candidate, strict=True)))
            except ValueError as error:
                raise ValueError(
                    f"{item!r} is not an IP address or CIDR network: {error}"
                ) from error
        return _unique(tuple(normalized), "source")

    @field_validator("allowed_countries", "denied_countries")
    @classmethod
    def validate_countries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = []
        for item in values:
            candidate = item.strip().upper()
            if not _COUNTRY.fullmatch(candidate):
                raise ValueError(
                    f"{item!r} is not an ISO 3166-1 alpha-2 country code such as 'DE'"
                )
            normalized.append(candidate)
        return _unique(tuple(normalized), "country")

    @field_validator("denied_methods")
    @classmethod
    def validate_methods(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = []
        for item in values:
            candidate = item.strip().upper()
            if not _HTTP_METHOD.fullmatch(candidate):
                raise ValueError(f"{item!r} is not an HTTP method such as 'DELETE'")
            normalized.append(candidate)
        return _unique(tuple(normalized), "method")

    @field_validator("denied_paths")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Accept a plain URI prefix and nothing that could end a directive.

        These are interpolated into a ``location`` in the generated config, so
        the characters nginx uses to close one — and the whitespace that would
        introduce a second argument — cannot appear.
        """
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
        return _unique(tuple(normalized), "path")

    @model_validator(mode="after")
    def validate_country_rules(self) -> Self:
        """Refuse a pair of country lists that can only deny everything.

        An allow list is emitted as "deny anything not in this list", so an
        entry in both lists is unreachable in one of them. Rather than pick a
        winner, say so — the operator meant one or the other.
        """
        overlap = set(self.allowed_countries) & set(self.denied_countries)
        if overlap:
            raise ValueError(
                "allowed_countries and denied_countries both list "
                + ", ".join(sorted(overlap))
            )
        return self

    @property
    def empty(self) -> bool:
        """True when this block imposes nothing, so it can be left unsent."""
        return not any(getattr(self, field) for field in SiteFirewall.model_fields)

    @property
    def requires_geoip(self) -> bool:
        return bool(self.allowed_countries or self.denied_countries)


def _unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    deduplicated = tuple(dict.fromkeys(values))
    if len(deduplicated) != len(values):
        raise ValueError(f"duplicate {label} entries are not allowed")
    return deduplicated


class SitePolicy(BaseModel):
    """How a hostname is served once it is proxied.

    Declared once and inherited, because the same block appears three times:
    on ``CdnSite``, on the ``DnsRecord`` that derives it, and — as all-optional
    fields — on ``RecordPatch``. Adding a knob in one place and forgetting the
    others produces a setting an operator can set and never see applied, which
    is exactly the failure the contract test cannot catch. ``RecordPatch``
    cannot inherit (every field has to become optional), so ``test_domain.py``
    asserts its field names still match this class.

    Inheriting puts these fields ahead of the identity fields in ``model_dump``
    order. Nothing consumes them positionally — the desired-state document is a
    mapping — so only the shape of the generated YAML changes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin_port: int | None = Field(default=None, ge=1, le=65535)
    origin_scheme: OriginScheme = OriginScheme.HTTPS
    origin_request_host: str | None = None
    origin_sni: str | None = None
    enabled: bool = True
    certificate_mode: CertificateMode = CertificateMode.DISABLED
    certificate_path: str | None = None
    certificate_key_path: str | None = None
    cache_enabled: bool = True
    cache_valid_success: str = "10m"
    cache_valid_not_found: str = "1m"
    #: Nested rather than flattened into six more fields, so a PATCH that
    #: touches the firewall replaces the whole block. Merging partial rule
    #: lists would make "remove the last deny" impossible to express.
    firewall: SiteFirewall = SiteFirewall()

    @field_validator("origin_request_host", "origin_sni")
    @classmethod
    def validate_optional_host(cls, value: str | None) -> str | None:
        return _hostname(value) if value is not None else None

    @field_validator("cache_valid_success", "cache_valid_not_found")
    @classmethod
    def validate_duration(cls, value: str) -> str:
        if not _DURATION.fullmatch(value):
            raise ValueError(
                "duration must be a non-negative integer followed by "
                "ms, s, m, h, d, or w"
            )
        return value


class CdnSite(SitePolicy):
    """Validated, provider-independent desired state for one CDN virtual host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    server_names: tuple[str, ...] = Field(min_length=1, max_length=100)
    origin_host: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _SITE_NAME.fullmatch(normalized):
            raise ValueError(
                "name must start with a letter and contain only a-z, 0-9, and hyphens"
            )
        return normalized

    @field_validator("server_names")
    @classmethod
    def validate_server_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            dict.fromkeys(_hostname(item, wildcard=True) for item in values)
        )
        if len(normalized) != len(values):
            raise ValueError("server_names must be unique")
        return normalized

    @field_validator("origin_host")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        return _hostname(value)

    @field_validator("certificate_path", "certificate_key_path")
    @classmethod
    def validate_remote_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("/") or ".." in value.split("/"):
            raise ValueError(
                "certificate paths must be absolute and cannot contain '..'"
            )
        if not value.startswith(_CERTIFICATE_ROOTS):
            raise ValueError(
                "certificate paths must live under one of: "
                + ", ".join(_CERTIFICATE_ROOTS)
            )
        return value

    @model_validator(mode="after")
    def validate_certificate_pair(self) -> Self:
        """Keep certificate mode and the two paths mutually consistent.

        Turning TLS off means clearing ``certificate_mode``, ``certificate_path``
        and ``certificate_key_path`` together in a single update; a patch that
        only sets the mode to ``disabled`` is rejected.
        """
        supplied = (
            self.certificate_path is not None or self.certificate_key_path is not None
        )
        if self.certificate_mode is not CertificateMode.DISABLED and not (
            self.certificate_path and self.certificate_key_path
        ):
            raise ValueError("TLS certificate modes require both certificate paths")
        if self.certificate_mode is CertificateMode.DISABLED and supplied:
            raise ValueError("certificate paths require certificate_mode='existing'")
        if self.certificate_mode in {
            CertificateMode.UPLOADED,
            CertificateMode.REQUESTED,
        } and (self.certificate_path, self.certificate_key_path) != (
            managed_certificate_paths(self.name)
        ):
            raise ValueError(
                f"certificate_mode={self.certificate_mode.value!r} is set by the "
                "certificate upload and request endpoints, which own the paths "
                f"under {MANAGED_TLS_ROOT}/<site>/"
            )
        return self

    def to_ansible(self) -> dict[str, Any]:
        document = self.model_dump(mode="json", exclude_none=True)
        # An untouched firewall is dropped rather than sent as six empty lists.
        # The desired state is read by operators during an incident, and a
        # block that appears on every site says nothing about the one site that
        # actually filters. The role defaults the key back to empty.
        if self.firewall.empty:
            del document["firewall"]
        return document


class RecordType(StrEnum):
    A = "A"
    AAAA = "AAAA"


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
        normalized = _hostname(value)
        try:
            ipaddress.ip_address(normalized)
        except ValueError:
            if "." not in normalized:
                raise ValueError(
                    "domain must be a delegable zone such as 'example.com'"
                ) from None
            return normalized
        raise ValueError("domain must be a name, not an IP address")


def derive_site_name(fqdn: str) -> str:
    """Return the ``CdnSite.name`` a proxied record derives.

    Site names are an internal identifier constrained to ``_SITE_NAME``, which
    admits neither dots nor ``*``. The mapping has to be deterministic: the
    certificate store is keyed by site name (``certificates.py``), so a name
    that changed between runs would orphan a live certificate.

    Collisions are possible in principle — ``a.b.example.com`` and
    ``a-b.example.com`` both flatten to ``a-b-example-com`` — so
    ``ControlPlane.validate()`` rejects two records that derive the same name
    rather than letting one silently overwrite the other.
    """
    slug = fqdn.replace("*", "wildcard").replace(".", "-")
    if not slug[:1].isalpha():
        slug = f"x-{slug}"
    if len(slug) > 63:
        digest = hashlib.blake2s(fqdn.encode("utf-8"), digest_size=4).hexdigest()[:7]
        slug = f"{slug[:55]}-{digest}"
    return slug


class DnsRecord(SitePolicy):
    """One record in a zone, and the CDN policy for it when proxied.

    ``proxied`` is the CDN on/off switch. Proxied, the edge serves this
    hostname and ``value`` is the origin it fetches from. Unproxied, the edge
    does not know the hostname exists and ``value`` is simply what DNS answers
    with — the record still belongs to us, it just bypasses the proxy.

    The CDN fields below are only meaningful while ``proxied`` is true. They
    are kept rather than cleared when it is turned off, so flipping the switch
    back does not silently lose a cache or TLS setting.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str
    name: str
    type: RecordType = RecordType.A
    value: str
    ttl: int = Field(default=300, ge=1, le=604800)
    proxied: bool = False

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        return _hostname(value)

    @field_validator("name")
    @classmethod
    def validate_record_name(cls, value: str) -> str:
        """``@`` is the zone apex, ``*`` a wildcard, anything else a subdomain."""
        normalized = value.strip().lower().rstrip(".")
        if normalized in {"@", "*"}:
            return normalized
        if not normalized:
            raise ValueError("record name cannot be empty; use '@' for the apex")
        if not all(_DNS_LABEL.fullmatch(label) for label in normalized.split(".")):
            raise ValueError(f"invalid record name: {value!r}")
        return normalized

    @model_validator(mode="after")
    def validate_value_matches_type(self) -> Self:
        try:
            address = ipaddress.ip_address(self.value.strip())
        except ValueError:
            raise ValueError(
                f"{self.type.value} record value must be an IP address"
            ) from None
        expected = 4 if self.type is RecordType.A else 6
        if address.version != expected:
            raise ValueError(
                f"{self.type.value} record value must be an IPv{expected} address"
            )
        return self

    @model_validator(mode="after")
    def validate_the_site_it_derives(self) -> Self:
        """Reject a proxied record that cannot produce a valid site.

        ``CdnSite`` owns the certificate-path rules that keep a deploy from
        writing as root outside ``/etc/blitzecdn/tls``, ``/etc/ssl`` and
        ``/etc/letsencrypt``. Deriving here means a record carrying a path
        outside them is refused when it is written, rather than persisting and
        then failing later when the derived table is rebuilt.
        """
        self.to_site()
        return self

    @property
    def fqdn(self) -> str:
        """The hostname this record answers for."""
        if self.name == "@":
            return self.domain
        return f"{self.name}.{self.domain}"

    @property
    def site_name(self) -> str:
        return derive_site_name(self.fqdn)

    def to_site(self) -> CdnSite | None:
        """Derive the edge virtual host, or ``None`` when the CDN is bypassed.

        Returning ``None`` is what takes the hostname off the edge entirely.
        The caller must not substitute a disabled site: a disabled site still
        occupies its name, whereas an unproxied record should leave no trace in
        the desired state at all.
        """
        if not self.proxied:
            return None
        # Copied by name off the shared policy rather than listed out, so a new
        # setting reaches the edge the moment it is added to ``SitePolicy``.
        return CdnSite(
            name=self.site_name,
            server_names=(self.fqdn,),
            origin_host=self.value,
            **{field: getattr(self, field) for field in SitePolicy.model_fields},
        )


class RecordPatch(BaseModel):
    """A partial update to a record: every field optional, unset means untouched.

    This cannot inherit ``SitePolicy`` — each field has to become optional, and
    an inherited required field would silently gain a default here. It is
    written out instead, and ``test_models.py`` fails if the two drift apart.
    """

    model_config = ConfigDict(extra="forbid")

    value: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=604800)
    proxied: bool | None = None
    origin_port: int | None = Field(default=None, ge=1, le=65535)
    origin_scheme: OriginScheme | None = None
    origin_request_host: str | None = None
    origin_sni: str | None = None
    enabled: bool | None = None
    certificate_mode: CertificateMode | None = None
    certificate_path: str | None = None
    certificate_key_path: str | None = None
    cache_enabled: bool | None = None
    cache_valid_success: str | None = None
    cache_valid_not_found: str | None = None
    # Replaces the block wholesale; see the note on SitePolicy.firewall. Send
    # {"firewall": {}} to clear every rule.
    firewall: SiteFirewall | None = None


class Deployment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    status: DeploymentStatus
    operator: str
    check_mode: bool
    #: Host pattern this run was narrowed to, or ``None`` for every edge.
    #:
    #: Recorded rather than derived because it changes what a green result
    #: means. A canary that succeeded against one edge is not evidence the
    #: fleet converged, and a rollback targeting it would restore a snapshot
    #: most edges never received — which is why ``successful_rollback_target``
    #: skips limited runs.
    host_limit: str | None = None
    rollback_of: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""


class OriginCheck(BaseModel):
    """The result of connecting to one site's origin the way the edge will.

    Three separate answers, because the fixes differ. ``resolved`` false means
    DNS; ``reachable`` false means routing, a firewall, or the wrong port;
    ``tls_verified`` false means the origin's certificate does not match the
    SNI the edge will send. ``tls_verified`` is ``None`` for an HTTP origin,
    where the question does not arise.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    origin: str
    scheme: OriginScheme
    sni: str | None = None
    resolved: bool = False
    reachable: bool = False
    tls_verified: bool | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and self.tls_verified is not False


class HostDrift(BaseModel):
    """What one edge would change if the current desired state were applied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    changed: int = 0
    ok: int = 0
    failed: int = 0
    unreachable: int = 0

    @property
    def in_sync(self) -> bool:
        return self.changed == 0 and self.failed == 0 and self.unreachable == 0


class DriftReport(BaseModel):
    """Whether the fleet still matches the state the control plane declares.

    Deploy answers "make it so"; this answers "is it still so". It is derived
    from a check-mode run, so ``changed`` counts tasks that *would* act, not
    tasks that did.

    Two honest limits on reading it. A host that is unreachable is reported as
    such rather than as drift — we did not learn anything about its
    configuration. And a task the role skips under check mode cannot be
    counted, so this floors rather than exactly measures the difference: it
    reliably tells you drift exists, and undercounts rather than invents it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment_id: str
    checked_at: datetime
    host_limit: str | None = None
    hosts: tuple[HostDrift, ...] = ()

    @property
    def in_sync(self) -> bool:
        """True only if every host was reached and none of them would change."""
        return bool(self.hosts) and all(host.in_sync for host in self.hosts)

    @property
    def drifted(self) -> tuple[HostDrift, ...]:
        return tuple(host for host in self.hosts if host.changed and not host.failed)

    @property
    def unreachable(self) -> tuple[HostDrift, ...]:
        return tuple(host for host in self.hosts if host.unreachable)


class PurgeEntry(BaseModel):
    """One cached response to remove, named the way a client would request it.

    Deliberately a hostname and a path rather than a site name: the cache is
    keyed by the ``Host`` header nginx saw, and a site can answer to several
    hostnames. Purging "the site" would have to purge every one of them and
    still could not express "only the apex".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    uri: str
    scheme: OriginScheme = OriginScheme.HTTPS

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return _hostname(value)

    @field_validator("uri")
    @classmethod
    def validate_uri(cls, value: str) -> str:
        """Accept the request target nginx would have logged, and nothing else.

        No normalization beyond stripping: the cache key is the raw
        ``$request_uri``, so ``/a/./b`` and ``/a/b`` are genuinely different
        entries and "helpfully" collapsing them here would purge a key that was
        never stored while leaving the real one in place.
        """
        candidate = value.strip()
        if not candidate.startswith("/"):
            raise ValueError("uri must be an absolute path beginning with '/'")
        if len(candidate) > 2048:
            raise ValueError("uri must be at most 2048 characters")
        if any(character.isspace() for character in candidate):
            raise ValueError("uri cannot contain whitespace")
        return candidate

    def to_ansible(self) -> dict[str, str]:
        return {"host": self.host, "uri": self.uri, "scheme": self.scheme.value}


class PurgeResult(BaseModel):
    """Which edges carried out a purge, and which did not.

    ``purged`` counts hosts that ran the removal, not entries deleted: nginx
    open source cannot report whether a key was present, so "deleted 3 files"
    and "there was nothing to delete" are the same observation. Treat a
    successful purge as "this object is not being served from cache any more",
    which is the question actually being asked, rather than as proof it was
    there.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    purged_at: datetime
    entries: tuple[PurgeEntry, ...] = ()
    purge_all: bool = False
    host_limit: str | None = None
    hosts: tuple[HostDrift, ...] = ()

    @property
    def succeeded(self) -> tuple[HostDrift, ...]:
        return tuple(
            host for host in self.hosts if not host.failed and not host.unreachable
        )

    @property
    def failed(self) -> tuple[HostDrift, ...]:
        return tuple(host for host in self.hosts if host.failed or host.unreachable)

    @property
    def complete(self) -> bool:
        """True only if every edge in scope purged.

        A partial purge is the dangerous outcome: the object is gone from some
        edges and still served by others, so which response a client gets
        depends on which edge answers.
        """
        return bool(self.hosts) and not self.failed


#: Cache outcomes nginx records in ``$upstream_cache_status`` that mean the
#: response came from cache. EXPIRED and BYPASS went to the origin; REVALIDATED
#: did too, but only to confirm the stored copy, so it is counted as a hit —
#: the origin sent no body and the edge served what it already had.
CACHE_HIT_OUTCOMES = frozenset({"HIT", "STALE", "UPDATING", "REVALIDATED"})

#: Outcomes that consulted the cache at all. A request logging an empty value
#: never reached the cache — a redirect, an nginx-generated error, or a site
#: with caching disabled — and is excluded from the ratio rather than counted
#: as a miss.
CACHE_CONSULTED_OUTCOMES = CACHE_HIT_OUTCOMES | {"MISS", "EXPIRED", "BYPASS"}


class SiteCacheStats(BaseModel):
    """Cache outcomes for one virtual host on one edge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    outcomes: dict[str, int] = Field(default_factory=dict)

    @property
    def requests(self) -> int:
        """Every logged request, including those that never used the cache."""
        return sum(self.outcomes.values())

    @property
    def cacheable_requests(self) -> int:
        return sum(
            count
            for outcome, count in self.outcomes.items()
            if outcome in CACHE_CONSULTED_OUTCOMES
        )

    @property
    def hits(self) -> int:
        return sum(
            count
            for outcome, count in self.outcomes.items()
            if outcome in CACHE_HIT_OUTCOMES
        )

    @property
    def hit_ratio(self) -> float | None:
        """Hits over requests that consulted the cache, or None if none did.

        None rather than 0.0 on purpose: a site with no cacheable traffic has
        no hit ratio, and reporting zero would make an idle site look like a
        broken one on a dashboard.
        """
        total = self.cacheable_requests
        return round(self.hits / total, 4) if total else None


class EdgeStats(BaseModel):
    """One edge's report: what it served, and what nginx itself counted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    collected_at: datetime | None = None
    nginx_reachable: bool = False
    #: stub_status counters, cumulative since nginx started. Deltas need two
    #: readings, which this does not attempt to store.
    connections: dict[str, int] = Field(default_factory=dict)
    sites: tuple[SiteCacheStats, ...] = ()
    #: Set when the edge produced no usable report at all.
    error: str | None = None

    @property
    def hits(self) -> int:
        return sum(site.hits for site in self.sites)

    @property
    def cacheable_requests(self) -> int:
        return sum(site.cacheable_requests for site in self.sites)

    @property
    def requests(self) -> int:
        return sum(site.requests for site in self.sites)

    @property
    def hit_ratio(self) -> float | None:
        total = self.cacheable_requests
        return round(self.hits / total, 4) if total else None


class CacheStatsReport(BaseModel):
    """Cache effectiveness across the fleet, as of one collection run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    collected_at: datetime
    host_limit: str | None = None
    edges: tuple[EdgeStats, ...] = ()

    @property
    def reporting(self) -> tuple[EdgeStats, ...]:
        return tuple(edge for edge in self.edges if edge.error is None)

    @property
    def silent(self) -> tuple[EdgeStats, ...]:
        return tuple(edge for edge in self.edges if edge.error is not None)

    @property
    def hits(self) -> int:
        return sum(edge.hits for edge in self.reporting)

    @property
    def cacheable_requests(self) -> int:
        return sum(edge.cacheable_requests for edge in self.reporting)

    @property
    def requests(self) -> int:
        return sum(edge.requests for edge in self.reporting)

    @property
    def hit_ratio(self) -> float | None:
        """Fleet hit ratio, weighted by request volume rather than by edge.

        Averaging the per-edge ratios would let an edge serving a hundred
        requests move the number as much as one serving a million.
        """
        total = self.cacheable_requests
        return round(self.hits / total, 4) if total else None

    def by_site(self) -> tuple[SiteCacheStats, ...]:
        """The same numbers summed across edges, which is how a site is judged.

        A single edge's ratio for a site says as much about which clients
        landed there as about the cache.
        """
        merged: dict[str, dict[str, int]] = {}
        for edge in self.reporting:
            for site in edge.sites:
                outcomes = merged.setdefault(site.site, {})
                for outcome, count in site.outcomes.items():
                    outcomes[outcome] = outcomes.get(outcome, 0) + count
        return tuple(
            SiteCacheStats(site=name, outcomes=outcomes)
            for name, outcomes in sorted(merged.items())
        )


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    created_at: datetime
    operator: str
    action: str
    resource_type: str
    resource_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class CertificateInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    source: CertificateSource
    domains: tuple[str, ...]
    not_before: datetime
    not_after: datetime
    fingerprint_sha256: str
    email: str | None = None


#: Renew an ACME certificate once it has this many days left.
#:
#: Let's Encrypt issues for 90 days and recommends renewing at 30, which
#: leaves two further windows to retry in before anything expires. Renewal
#: goes through HTTP-01, so a failure is usually a transient DNS or reachability
#: problem that a later attempt clears.
CERTIFICATE_RENEWAL_DAYS = 30


class CertificateStatus(BaseModel):
    """A stored certificate seen against the clock.

    Separate from ``CertificateInfo`` because that is the record written to
    ``metadata.json`` and read back under ``extra="forbid"`` — folding a
    computed field into it would make every stored file fail to reload. This
    is the derived view, built when someone asks.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    source: CertificateSource
    domains: tuple[str, ...]
    not_after: datetime
    #: Whole days until expiry, negative once it has passed.
    days_remaining: int
    expired: bool
    #: Whether BlitzeCDN can renew this one itself. Only ACME certificates
    #: qualify; an uploaded one has to be replaced by whoever supplied it.
    renewable: bool
    fingerprint_sha256: str

    @classmethod
    def of(cls, info: CertificateInfo, *, now: datetime) -> Self:
        remaining = info.not_after - now
        return cls(
            site=info.site,
            source=info.source,
            domains=info.domains,
            not_after=info.not_after,
            # Truncated toward minus infinity, so a certificate with six hours
            # left reports 0 rather than rounding up to a reassuring 1.
            days_remaining=remaining.days,
            expired=remaining.total_seconds() <= 0,
            renewable=info.source is CertificateSource.ACME,
            fingerprint_sha256=info.fingerprint_sha256,
        )

    def due_for_renewal(self, within_days: int = CERTIFICATE_RENEWAL_DAYS) -> bool:
        return self.renewable and self.days_remaining <= within_days


#: A proxied record with a TTL above this is flagged before issuance.
#:
#: Not an error — a long TTL is legitimate for a hostname that has been live
#: for months. It matters only around a cutover: resolvers that cached the old
#: address keep answering with it for up to the TTL, and HTTP-01 validation
#: follows public DNS, so a slow rollover shows up as an issuance failure with
#: no obvious cause.
TTL_CUTOVER_ADVISORY_SECONDS = 3600


class PreflightSeverity(StrEnum):
    """Whether a failed check stops issuance or is merely reported.

    ``BLOCKING`` is reserved for conditions under which HTTP-01 validation
    cannot succeed, so failing early costs nothing and spares the CA's rate
    limit. Everything a certificate can legitimately be issued in spite of —
    an origin that is down, a TTL that is high — is ``ADVISORY``.
    """

    BLOCKING = "blocking"
    ADVISORY = "advisory"


class PreflightCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Stable identifier (``dns``, ``caa``, ``deployed``, ``origin``, ``ttl``),
    #: safe for a caller to branch on. ``detail`` is prose and is not.
    name: str
    passed: bool
    severity: PreflightSeverity
    detail: str


class PreflightReport(BaseModel):
    """What the outside world looks like just before we ask a CA for a cert."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    checks: tuple[PreflightCheck, ...]

    @property
    def blocking_failures(self) -> tuple[PreflightCheck, ...]:
        return tuple(
            check
            for check in self.checks
            if not check.passed and check.severity is PreflightSeverity.BLOCKING
        )

    @property
    def advisories(self) -> tuple[PreflightCheck, ...]:
        return tuple(
            check
            for check in self.checks
            if not check.passed and check.severity is PreflightSeverity.ADVISORY
        )

    @property
    def ok(self) -> bool:
        """True when nothing blocks issuance. Advisories do not count."""
        return not self.blocking_failures

    def summary(self) -> str:
        return "; ".join(
            f"{check.name}: {check.detail}" for check in self.blocking_failures
        )


class CertificateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(default=None, max_length=254)
    #: Issue even though preflight found a blocking problem. Deliberately not
    #: the default: every blocking check describes a condition under which the
    #: CA cannot validate, so the usual effect of overriding is to spend a rate
    #: limit on a request that fails anyway. It exists for the case the
    #: controller cannot see — split-horizon DNS, or an edge fronted by an
    #: address the inventory does not name.
    skip_preflight: bool = False

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip().lower()
        if candidate.count("@") != 1 or any(char.isspace() for char in candidate):
            raise ValueError("email must be a valid address")
        local, domain = candidate.rsplit("@", 1)
        if not local or not domain:
            raise ValueError("email must be a valid address")
        _hostname(domain)
        return candidate
