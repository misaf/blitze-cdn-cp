"""Results produced by Cloudflare-style Automatic SSL/TLS scans."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.capabilities.deployments.domain import Deployment
from blitzecdn.capabilities.tls.policy import SslMode


class SslAutomaticReconciliation(BaseModel):
    """One fleet scan and the upgrade-only decisions it made."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scanned: tuple[str, ...] = ()
    upgraded: dict[str, SslMode] = Field(default_factory=dict)
    skipped: dict[str, str] = Field(default_factory=dict)
    deployment: Deployment | None = None
