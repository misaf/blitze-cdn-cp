"""A bounded read result, independent of the delivery framework."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PageResult[T]:
    items: list[T]
    total: int
