"""Declare zones, their policy, their rules, their records — and what edges get.

The flat site document every edge role reads is rendered by
:mod:`blitzecdn.capabilities.dns.runtime`. It
was `sites`' contribution when a site was a thing an operator authored; the
capability that composes a virtual host is the one that projects it, and that
is this one.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.dns import cli
from blitzecdn.capabilities.dns.api import routes
from blitzecdn.core.features import CliCommandGroup, Feature, FeatureMetadata

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="dns",
            version=__version__,
            summary="DNS zones, their policy, the rules on it, and their records.",
        ),
        api_routers=(routes.router,),
        cli_commands=(
            CliCommandGroup(feature="dns", name="domain", app=cli.domain_app),
            CliCommandGroup(feature="dns", name="record", app=cli.record_app),
            CliCommandGroup(feature="dns", name="rule", app=cli.rule_app),
            CliCommandGroup(feature="dns", name="dns", app=cli.dns_app),
        ),
    ),
)
