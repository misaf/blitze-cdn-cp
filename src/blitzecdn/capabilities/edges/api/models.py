"""The HTTP representations this capability publishes."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from blitzecdn.capabilities.edges.domain import Edge as DomainEdge
from blitzecdn.capabilities.edges.domain import EdgePatch as DomainEdgePatch
from blitzecdn.entrypoints.api.models import HostRun, Model


class Edge(Model):
    name: str
    host: str
    user: str = "deploy"
    port: int = Field(default=22, ge=1, le=65535)
    private_key_file: str | None = None
    public_addresses: tuple[str, ...] = Field(default=(), max_length=20)
    ssh_sources: tuple[str, ...] = Field(default=(), max_length=100)
    #: Capability tokens this edge's runtime provides; ``None`` means the
    #: controller's installed set.
    capabilities: tuple[str, ...] | None = Field(default=None, max_length=50)

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
    capabilities: tuple[str, ...] | None = Field(default=None, max_length=50)

    def to_domain(self) -> DomainEdgePatch:
        return DomainEdgePatch.model_validate(self.model_dump(exclude_unset=True))


class EdgeRemoval(Model):
    name: str
    decommissioned: bool
    hosts: tuple[HostRun, ...] = ()


__all__ = ["Edge", "EdgePatch", "EdgeRemoval"]
