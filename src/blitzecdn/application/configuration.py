"""Small immutable policy objects consumed by application use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DeploymentPolicy:
    run_dir: Path
    generated_vars_path: Path
    output_limit_bytes: int
    history_retention: int
    runtime_errors: Callable[[], list[str]]


@dataclass(frozen=True)
class CertificatePolicy:
    default_email: str | None
