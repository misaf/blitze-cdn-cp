"""The published representation of an Automatic SSL/TLS scan."""

from pydantic import ConfigDict, Field

from blitzecdn.capabilities.deployments import DeploymentStatus
from blitzecdn.capabilities.tls.contract import SslMode
from blitzecdn.delivery.api.models import Model


class DeploymentReference(Model):
    """The deployment a scan started, as Automatic SSL/TLS reports it.

    This capability owns the representation rather than reusing the
    deployments API's: a change to how deployments are published must not
    change this response. Follow ``GET /v3/deployments/{id}`` for the record.
    """

    # Built from the full deployment record; the fields it leaves out are the
    # deployments API's to publish, not unknown input.
    model_config = ConfigDict(extra="ignore")

    id: str
    status: DeploymentStatus
    release_id: str
    error: str | None = None


class SslAutomaticReconciliation(Model):
    scanned: tuple[str, ...] = ()
    upgraded: dict[str, SslMode] = Field(default_factory=dict)
    skipped: dict[str, str] = Field(default_factory=dict)
    deployment: DeploymentReference | None = None
