from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol


class UnitOfWork(Protocol):
    """One atomic boundary shared by every store used by a use case."""

    def transaction(self) -> AbstractContextManager[Any]: ...


__all__ = ["UnitOfWork"]
