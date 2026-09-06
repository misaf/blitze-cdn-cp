"""Register the stable TLS policy capability.

Metadata and nothing else, which is the whole point. Everything TLS *does* —
issuing, renewing, publishing, and overriding the two certificate paths a site
projects with the fingerprinted files the material is actually stored under —
went with ``blitzecdn-certificates``, where the code that knows those paths
lives. What is left here is the contract: ``TlsPolicy``, composed into
``CdnSite`` by ``sites``, which is an import rather than a hook.

A capability with nothing to contribute still registers, because the name is
what a refusal is written in. A site whose ``ssl_mode`` needs issuance is
refused by ``capability_requirements`` naming ``tls``, and a name no plugin
claims cannot be resolved to a summary, a version, or an answer to "installed?"
— so the registration is the declaration that this capability exists, not a
vestige of the contributions that left.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.core.plugins import PluginMetadata, hookimpl


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="tls",
        version=__version__,
        api_version=1,
        required=True,
        summary="Stable edge-encryption policy and TLS modes.",
    )
