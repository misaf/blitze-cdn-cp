"""The images BlitzeCDN builds, and where their build inputs are on disk.

Shipped in the package so an air-gapped controller can build its edge image.
There is no control-plane build context here: it is the source tree itself.
"""

from __future__ import annotations

from pathlib import Path

from blitzecdn.core.runtime.resources import package_directory

__all__ = [
    "CONTROL_PLANE_DOCKERFILE",
    "CONTROL_PLANE_DOCKERIGNORE",
    "EDGE_CONTEXT",
    "EDGE_DOCKERFILE",
    "EDGE_MODULE_PROBE_CONF",
]


_DIRECTORY = package_directory(
    __name__,
    resolves="Docker resolves a build context and a Dockerfile by filesystem path",
)


#: The edge image's complete build context: ``docker build`` this path.
EDGE_CONTEXT = _DIRECTORY / "edge"

#: Named separately for the contract tests and for a caller that wants to pass
#: ``--file`` explicitly; it is the default Dockerfile of :data:`EDGE_CONTEXT`.
EDGE_DOCKERFILE = EDGE_CONTEXT / "Dockerfile"

#: The build-time probe frame. Module lists and probe directives are generated
#: during the build from what the capabilities declare; see
#: :class:`blitzecdn.core.features.EdgeModule`.
EDGE_MODULE_PROBE_CONF = EDGE_CONTEXT / "module-probe.conf"

#: The control plane's Dockerfile. Its context is the source tree (``uv sync
#: --frozen`` needs ``pyproject.toml``, ``uv.lock`` and ``src/``), supplied by
#: the role that builds it.
CONTROL_PLANE_DOCKERFILE = _DIRECTORY / "control-plane" / "Dockerfile"

#: BuildKit finds `<dockerfile>.dockerignore` beside the Dockerfile, so it
#: lives here.
CONTROL_PLANE_DOCKERIGNORE = Path(f"{CONTROL_PLANE_DOCKERFILE}.dockerignore")
