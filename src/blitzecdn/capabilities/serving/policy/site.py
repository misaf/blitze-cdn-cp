"""Compose serving policy independently of DNS storage and lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from pydantic import ConfigDict, model_validator

from blitzecdn.capabilities.cache.policy import CachePolicy
from blitzecdn.capabilities.compression.policy import CompressionPolicy
from blitzecdn.capabilities.http.policy import ProtocolPolicy
from blitzecdn.capabilities.security.policy import SecurityPolicy
from blitzecdn.capabilities.serving.policy.headers import HeaderPolicy
from blitzecdn.capabilities.serving.policy.origin import OriginPolicy
from blitzecdn.capabilities.tls.policy import TlsPolicy
from blitzecdn.core.domain.policy import CapabilityPolicy


class SitePolicy(
    TlsPolicy,
    ProtocolPolicy,
    OriginPolicy,
    CachePolicy,
    CompressionPolicy,
    SecurityPolicy,
    HeaderPolicy,
):
    """How a hostname is served once a site answers for it.

    Declared once and inherited, because the same block appears twice: on
    ``CdnSite`` and — as all-optional fields — on ``DomainPatch``. Adding a knob
    to one and forgetting the other produces a setting an operator can set once
    and never change again, which is exactly the failure the contract test
    cannot catch. ``DomainPatch`` cannot inherit (every field has to become
    optional), so ``_assert_patch_covers_zone`` in
    :mod:`~blitzecdn.capabilities.dns.domain.patch` refuses to import a
    version of that module where the two have drifted apart.

    Inheriting puts these fields ahead of the identity fields in ``model_dump``
    order. Nothing consumes them positionally — the desired-state document is a
    mapping — so only the shape of the generated YAML changes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """Optional capability tokens, and the settings that asked for each.

        The single place a site's dependency on a detachable implementation is
        *answered*, and nowhere any of it is decided: each contract declares its
        own, and this merges them. There is no capability name in
        this module at all — not in a branch, not in a token, not in a list of
        contracts — so a capability this repository has never heard of travels
        the same path as `compression`, and a new one is a property on its own
        contract rather than a second edit here.

        The contracts are read off the MRO rather than listed, for that reason:
        what a list produces when someone forgets it is a token that is
        silently never requested, and a deployment that proceeds without the
        distribution that was supposed to serve it.

        The settings travel with the token because "capability 'geoip' is not
        installed" alone leaves an operator hunting for which of two unrelated
        switches asked for it. Names are the stable schema's own field names,
        so what the message says to change is what a patch would set. Two
        contracts may ask for one token and both sets of names survive; order
        follows the bases as declared, so the message does not depend on which
        switch happened to be set first.

        A disabled site converges no server block, so it requests nothing.
        """
        if not self.enabled:
            return {}
        requested: dict[str, list[str]] = {}
        for contract in type(self).__mro__:
            if contract in (SitePolicy, CapabilityPolicy):
                continue
            declared = contract.__dict__.get("capability_requirements")
            if not isinstance(declared, property) or declared.fget is None:
                continue
            for token, settings in declared.fget(self).items():
                requested.setdefault(token, []).extend(settings)
        return {name: tuple(settings) for name, settings in requested.items()}

    @property
    def required_capabilities(self) -> frozenset[str]:
        """Optional implementation tokens requested by the stable schema."""
        return frozenset(self.capability_requirements)

    @model_validator(mode="after")
    def validate_http3_requires_edge_tls(self) -> Self:
        if self.http3_enabled and not self.ssl_mode.serves_tls:
            raise ValueError("http3_enabled=True requires ssl_mode to serve edge TLS")
        return self

    @property
    def requires_geoip(self) -> bool:
        """Whether serving this site needs ``$blitzecdn_country`` to exist.

        Firewall and visitor-header contracts declare whether they need the
        country lookup on their own models.

        Ungated by ``enabled`` on purpose. This is what the edge role's
        validation asks of a document it was handed; whether the site converges
        at all is a separate question, and ``capability_requirements`` is the
        one that asks it.
        """
        return self.firewall.requires_geoip or self.visitor_headers.requires_geoip
