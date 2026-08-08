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
DESIRED_STATE_VERSION = 1

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
        return self.model_dump(mode="json", exclude_none=True)


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


class CertificateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(default=None, max_length=254)

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
