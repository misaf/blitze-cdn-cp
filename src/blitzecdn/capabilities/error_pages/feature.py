"""Declare the application's error_pages capability."""

from __future__ import annotations

from blitzecdn.capabilities.error_pages import cli
from blitzecdn.capabilities.error_pages.contract import ErrorPagesPolicy
from blitzecdn.core.features import (
    CliCommandGroup,
    Feature,
    FeatureMetadata,
    NginxContribution,
    PolicyContribution,
)
from blitzecdn.core.runtime.resources import distribution_version, package_directory

#: The installed distribution's version, read from package metadata.
__version__ = distribution_version(__name__)

#: The Nginx fragments and pages this capability contributes, rendered into
#: each release on the controller rather than shipped inside a role.
NGINX_TEMPLATES = (
    package_directory(
        __name__,
        resolves="Nginx templates are rendered from a filesystem path",
    )
    / "nginx"
)

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="error_pages",
            version=__version__,
            provides=frozenset({"error_pages"}),
            summary="Per-site error pages and maintenance mode.",
        ),
        cli_commands=(
            CliCommandGroup(
                feature="error_pages",
                name="domain",
                app=cli.domain_commands,
                extends=True,
            ),
        ),
        nginx=(
            NginxContribution(
                feature="error_pages",
                templates_path=NGINX_TEMPLATES,
                http_fragments=("error-pages-http.conf.j2",),
                server_fragments=("error-pages-server.conf.j2",),
                access_fragments=("error-pages-access.conf.j2",),
                upstream_fragments=("error-pages-upstream.conf.j2",),
                # One file per page a site uses; an unused one renders empty
                # and is not written.
                page_fragments=(
                    "403.html.j2",
                    "429.html.j2",
                    "500.html.j2",
                    "502.html.j2",
                    "503.html.j2",
                    "504.html.j2",
                    "maintenance.html.j2",
                ),
            ),
        ),
        site_policy=(
            PolicyContribution(
                feature="error_pages", group="error_pages", contract=ErrorPagesPolicy
            ),
        ),
    ),
)
