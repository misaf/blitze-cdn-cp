"""The names control-plane values are keyed by, constrained once.

Four aliases, each the shape of something the control plane identifies rather
than the thing itself: an operator, a deployment, an edge. They are core's
because more than one capability keys a value by them and none of them decides
what a valid one looks like.

They lived in `core.domain.operations` beside the workflow journal, which made
the module two things at once — a vocabulary of identifiers, and one capability's
domain — and the second of those has moved to `capabilities/workflows/`.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
DeploymentId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
EdgeName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+$")]
Operator = Identifier

__all__ = ["DeploymentId", "EdgeName", "Identifier", "Operator"]
