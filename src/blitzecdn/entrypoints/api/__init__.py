"""HTTP entry point for the BlitzeCDN control plane."""

from blitzecdn.entrypoints.api.app import create_app

__all__ = ["create_app"]
