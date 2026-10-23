"""Canonical JSON for content-addressed values.

The stored string is authoritative. Consumers decode their own working copy;
editing it cannot change the identity of an artifact already admitted to a
deployment. Unsupported objects are refused instead of converted with `str`,
which could hide a path, a process-specific representation, or a missing encoder.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def canonical_document(value: object) -> str:
    """Encode a JSON object, or canonicalize an encoded object."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("a document must be a JSON object")
    _validate(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def read_document(content: str) -> dict[str, Any]:
    """A fresh mutable view of immutable canonical content."""
    result: dict[str, Any] = json.loads(content)
    return result


def _validate(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            _validate(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _validate(item)
    elif value is not None and not isinstance(value, str | bool | int | float):
        raise ValueError(f"unsupported JSON value: {type(value).__name__}")
