"""The published representation of an Automatic SSL/TLS scan."""

from pydantic import Field

from blitzecdn.capabilities.deployments.api.models import Deployment
from blitzecdn.capabilities.tls.contract import SslMode
from blitzecdn.entrypoints.api.models import Model


class SslAutomaticReconciliation(Model):
    scanned: tuple[str, ...] = ()
    upgraded: dict[str, SslMode] = Field(default_factory=dict)
    skipped: dict[str, str] = Field(default_factory=dict)
    deployment: Deployment | None = None
