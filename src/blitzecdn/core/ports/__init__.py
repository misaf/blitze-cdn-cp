"""The protocols a service declares instead of naming an implementation.

`UnitOfWork` is the atomic boundary every store in a use case shares;
`ports.operations` holds the ports for work that leaves the process. A port
whose service, store and table all sit in one slice belongs to that slice.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol


class UnitOfWork(Protocol):
    """One atomic boundary shared by every store used by a use case."""

    def transaction(self) -> AbstractContextManager[Any]: ...


__all__ = ["UnitOfWork"]
