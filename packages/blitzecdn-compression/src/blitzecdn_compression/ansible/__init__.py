"""Filesystem location of the compression capability's edge role."""

from pathlib import Path

ROLES_PATH = Path(__file__).with_name("roles")
EDGE_ROLE = "blitzecdn_compression"

__all__ = ["EDGE_ROLE", "ROLES_PATH"]
