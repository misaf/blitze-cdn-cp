"""The HTTP representations this capability publishes.

``EdgeRemoval`` reports an operation rather than a resource, and is here for
the same reason ``Edge`` is: forgetting an edge is something `edges` does, so
what that answers with is `edges`' to change. The per-host detail inside it is
core's `HostRun`, because an Ansible run belongs to no capability.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from blitzecdn.api.models import HostRun, Model
from blitzecdn.capabilities.edges.domain import Edge as DomainEdge
from blitzecdn.capabilities.edges.domain import EdgePatch as DomainEdgePatch


class Edge(Model):
    name: str
    host: str
    user: str = "deploy"
    port: int = Field(default=22, ge=1, le=65535)
    private_key_file: str | None = None
    public_addresses: tuple[str, ...] = Field(default=(), max_length=20)
    ssh_sources: tuple[str, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def valid_edge(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> DomainEdge:
        return DomainEdge.model_validate(self.model_dump())

    @classmethod
    def from_domain(cls, value: DomainEdge) -> Self:
        return cls.model_validate(value.model_dump(mode="json"))


class EdgePatch(Model):
    host: str | None = None
    user: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    private_key_file: str | None = None
    public_addresses: tuple[str, ...] | None = None
    ssh_sources: tuple[str, ...] | None = None

    def to_domain(self) -> DomainEdgePatch:
        return DomainEdgePatch.model_validate(self.model_dump(exclude_unset=True))


class EdgeRemoval(Model):
    name: str
    decommissioned: bool
    hosts: tuple[HostRun, ...] = ()


__all__ = ["Edge", "EdgePatch", "EdgeRemoval"]
