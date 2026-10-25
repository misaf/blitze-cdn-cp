"""Declare the TLS capability: its stable policy, and certificate management.

Two features, because they answer to two capability tokens. ``tls`` is the
contract — ``TlsPolicy``, composed into ``CdnSite`` by serving — and declares
nothing but its name, because the name is what a refusal is written in: a site
whose ``ssl_mode`` needs issuance is refused by ``capability_requirements``
naming ``tls``. ``certificates`` issues, renews and runs Automatic SSL/TLS; its
rendering and operations are built in :mod:`blitzecdn.capabilities.tls.runtime`.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.tls.automatic_ssl import cli as automatic_ssl_cli
from blitzecdn.capabilities.tls.automatic_ssl.api import routes as automatic_ssl_routes
from blitzecdn.capabilities.tls.certificates import cli as certificates_cli
from blitzecdn.capabilities.tls.certificates.api import routes as certificates_routes
from blitzecdn.capabilities.tls.composition import (
    __version__ as certificates_version,
)
from blitzecdn.capabilities.tls.config import SETTINGS
from blitzecdn.core.plugins import (
    CliCommandGroup,
    ConfigurationContribution,
    Feature,
    PluginMetadata,
)

TLS = Feature(
    metadata=PluginMetadata(
        name="tls",
        version=__version__,
        summary="Stable edge-encryption policy and TLS modes.",
    ),
)

CERTIFICATES = Feature(
    metadata=PluginMetadata(
        name="certificates",
        version=certificates_version,
        provides=frozenset({"certificates"}),
        summary="Certificate upload, ACME renewal, and Automatic SSL/TLS.",
    ),
    api_routers=(certificates_routes.router, automatic_ssl_routes.router),
    cli_commands=(
        CliCommandGroup(
            plugin="certificates", name="cert", app=certificates_cli.cert_app
        ),
        CliCommandGroup(
            plugin="certificates", name="ssl", app=automatic_ssl_cli.ssl_app
        ),
    ),
    # Controller-side settings and no secrets, which is what a
    # `CapabilitySetting` is for.
    configuration=(
        ConfigurationContribution(plugin="certificates", settings=SETTINGS),
    ),
)

FEATURES = (TLS, CERTIFICATES)
