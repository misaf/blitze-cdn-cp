"""Declare the TLS capability: its policy contract, and certificate management.

``tls`` is the contract and declares only its name, the token refusals name.
``certificates`` issues, renews and runs Automatic SSL/TLS.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.tls import cli
from blitzecdn.capabilities.tls.automatic_ssl import cli as automatic_ssl_cli
from blitzecdn.capabilities.tls.automatic_ssl.api import routes as automatic_ssl_routes
from blitzecdn.capabilities.tls.certificates import cli as certificates_cli
from blitzecdn.capabilities.tls.certificates.api import routes as certificates_routes
from blitzecdn.capabilities.tls.settings import CONFIGURATION
from blitzecdn.capabilities.tls.wiring import (
    __version__ as certificates_version,
)
from blitzecdn.core.features import (
    CliCommandGroup,
    Feature,
    FeatureMetadata,
)

TLS = Feature(
    metadata=FeatureMetadata(
        name="tls",
        version=__version__,
        summary="Stable edge-encryption policy and TLS modes.",
    ),
    cli_commands=(
        CliCommandGroup(
            feature="tls",
            name="domain",
            app=cli.domain_commands,
            extends=True,
        ),
    ),
)

CERTIFICATES = Feature(
    metadata=FeatureMetadata(
        name="certificates",
        version=certificates_version,
        provides=frozenset({"certificates"}),
        summary="Certificate upload, ACME renewal, and Automatic SSL/TLS.",
    ),
    api_routers=(certificates_routes.router, automatic_ssl_routes.router),
    cli_commands=(
        CliCommandGroup(
            feature="certificates", name="cert", app=certificates_cli.cert_app
        ),
        CliCommandGroup(
            feature="certificates", name="ssl", app=automatic_ssl_cli.ssl_app
        ),
    ),
    configuration=(CONFIGURATION,),
)

FEATURES = (TLS, CERTIFICATES)
