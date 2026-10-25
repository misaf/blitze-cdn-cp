"""The protocols a service declares instead of naming an implementation.

`UnitOfWork` is here rather than in a module of its own because it is the
abstraction the package is about: one atomic boundary, shared by every store a
use case touches. `ports.operations` holds the ports for work that leaves the
process — the audit trail, the playbook runner, the event recorder. A port
whose service, store and table all sit in one slice belongs to that slice, not
here.

A port is a value-level contract, so this package imports `core.domain` and
nothing else — the `core-kernel` contract in `.importlinter` refuses this
package anything more.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol


class UnitOfWork(Protocol):
    """One atomic boundary shared by every store used by a use case."""

    def transaction(self) -> AbstractContextManager[Any]: ...


__all__ = ["UnitOfWork"]
