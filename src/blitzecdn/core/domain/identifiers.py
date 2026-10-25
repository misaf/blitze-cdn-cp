"""The names control-plane values are keyed by, constrained once."""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
DeploymentId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
EdgeName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+$")]
Operator = Identifier

__all__ = ["DeploymentId", "EdgeName", "Identifier", "Operator"]
