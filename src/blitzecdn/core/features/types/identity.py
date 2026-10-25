"""Who a feature is, and which capability tokens it answers for."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["FeatureMetadata"]


@dataclass(frozen=True, slots=True)
class FeatureMetadata:
    """Who a feature is, and which capability tokens it supplies."""

    name: str
    version: str
    summary: str = ""
    #: Capability tokens supplied beyond the feature's own name. `name`
    #: identifies the feature; a token is what a site or setting depends on,
    #: and one feature may supply several.
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
