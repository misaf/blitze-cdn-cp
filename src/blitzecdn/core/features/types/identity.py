"""Who a feature is, and which capability tokens it answers for."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["FeatureMetadata"]


@dataclass(frozen=True, slots=True)
class FeatureMetadata:
    """Who a feature is.

    Every feature is built in and required: one that fails to import stops the
    process at startup, because the control plane without it is not a degraded
    control plane but a wrong one — a `dns` that did not load would silently
    render an empty fleet.
    """

    name: str
    version: str
    summary: str = ""
    #: The capability tokens this feature supplies, for configuration that has
    #: to say what it depends on. Empty means "just my own name", which is the
    #: right answer for almost every feature: `backup` provides `backup`.
    #:
    #: It is separate from `name` because the two answer different questions.
    #: `name` identifies the *feature* — it is how a duplicate is detected and a
    #: failure attributed — while a token names a *capability*, which is what a
    #: site or a fleet setting actually depends on. One feature may supply
    #: several, and a replacement implementation may supply a token another
    #: package used to, under its own name.
    provides: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"feature name {self.name!r} must be alphanumeric")
        for token in self.provides:
            if not token or not token.replace("_", "").replace("-", "").isalnum():
                raise ValueError(f"capability {token!r} must be alphanumeric")

    @property
    def capabilities(self) -> frozenset[str]:
        """Every capability token this feature answers for, its name included."""
        return self.provides | {self.name}
