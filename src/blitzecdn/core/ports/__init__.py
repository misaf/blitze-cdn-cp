"""The protocols a service declares instead of naming an implementation.

`UnitOfWork` is here rather than in a module of its own because it is the
abstraction the package is about: one atomic boundary, shared by every store a
use case touches. `ports.operations` holds the ports for work that leaves the
process — the audit trail, the playbook runner, the event recorder. The
workflow journal was a fourth until the journal became a capability; a port
whose service, store and table all sit in one slice was that slice seen from
the outside, not a cross-cutting contract.

A port is a value-level contract, so this package imports `core.domain` and
nothing else — `test_core_domain_and_ports_are_framework_and_io_independent`
refuses this package anything more, which is what lets the SDK publish it whole
rather than one named module at a time. `core.ports` was one file and
`core.operation_ports` another, which named the same idea twice and put the
second one nowhere in particular.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol


class UnitOfWork(Protocol):
    """One atomic boundary shared by every store used by a use case."""

    def transaction(self) -> AbstractContextManager[Any]: ...


__all__ = ["UnitOfWork"]
