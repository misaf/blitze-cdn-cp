"""The images BlitzeCDN builds, and where their build inputs landed on disk.

The edge runtime's Dockerfile and the Nginx probe baked into it, and the
control plane's own Dockerfile, ship *inside this wheel* for the
same reason :mod:`blitzecdn.ansible` does: they are part of the deployment
implementation, and an installed controller has no repository to find them in.
Before this module existed they sat in a top-level ``docker/`` directory that
only a checkout has, which is the undeclared-checkout dependency the Ansible
move already removed once — the justfile, both integration scripts, the release
workflow and six contract tests each spelled the path again, and an air-gapped
fleet that has to build its edge image on the controller had nothing to build
from.

Located through :func:`blitzecdn.core.runtime.resources.package_directory`, not by
counting ``..`` from ``__file__``, so the paths are the same whether they are
read from a checkout or from a virtualenv on a controller.

What is deliberately *not* here is a build context for the control-plane image.
Its context is the whole distribution source, which is exactly the thing this
package must not name — see :data:`CONTROL_PLANE_DOCKERFILE`.
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


#: The edge runtime image's build context, and the whole of it. Everything the
#: image needs is in this directory, so a build is ``docker build`` against this
#: path with no ``--file`` and no wider context — which is what makes building
#: an edge image from an installed controller possible at all.
EDGE_CONTEXT = _DIRECTORY / "edge"

#: Named separately for the contract tests and for a caller that wants to pass
#: ``--file`` explicitly; it is the default Dockerfile of :data:`EDGE_CONTEXT`.
EDGE_DOCKERFILE = EDGE_CONTEXT / "Dockerfile"

#: The build-time probe that proves every module the image was built with
#: loads and registers a directive against the binary it was compiled for.
#:
#: There is no ``EDGE_MODULES_CONF`` beside it any more, and its absence is the
#: point. The ``load_module`` list used to be a file here, which made this
#: directory a register of which capabilities exist and left an edge loading a
#: module whose distribution had been detached. Both the list and the probe's
#: directives are now generated during the build from what the installed
#: capabilities declare — see :class:`blitzecdn.core.plugins.EdgeModule` — and
#: this file is only their frame.
EDGE_MODULE_PROBE_CONF = EDGE_CONTEXT / "module-probe.conf"

#: The control plane's own image. A path only — there is no
#: ``CONTROL_PLANE_CONTEXT`` beside it, because that context is the
#: distribution's source tree: ``uv sync --frozen`` inside the build needs
#: ``pyproject.toml``, ``uv.lock`` and every workspace member under
#: ``packages/``. Naming it here would put the checkout back into the package
#: that just stopped depending on one. The role that builds this image supplies
#: the context, and only until the control plane is delivered as a published
#: image like the edge already is.
CONTROL_PLANE_DOCKERFILE = _DIRECTORY / "control-plane" / "Dockerfile"

#: BuildKit resolves a Dockerfile-specific ignore file as ``<dockerfile>``
#: suffixed with ``.dockerignore``, falling back to ``.dockerignore`` at the
#: context root. It therefore has to travel with the Dockerfile rather than
#: with the context, which is why it is in this directory and not beside
#: ``pyproject.toml``.
CONTROL_PLANE_DOCKERIGNORE = Path(f"{CONTROL_PLANE_DOCKERFILE}.dockerignore")
