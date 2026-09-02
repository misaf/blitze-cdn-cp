"""Filesystem location of the HTTP/3 capability's edge role."""

from pathlib import Path

ROLES_PATH = Path(__file__).with_name("roles")
EDGE_ROLE = "blitzecdn_http3"

__all__ = ["EDGE_ROLE", "ROLES_PATH"]
