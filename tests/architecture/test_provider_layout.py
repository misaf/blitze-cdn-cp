from __future__ import annotations

from pathlib import Path

from support.paths import CORE_DOCKER, REPO_ROOT


def test_the_published_docker_paths_are_the_ones_in_the_checkout():
    """`blitzecdn.assets.docker` and the checkout tree are the same directory.

    In an editable install `importlib.resources` answers the checkout, so suites
    reading the constants read the files under review.
    """
    from blitzecdn.assets import docker

    assert docker.EDGE_CONTEXT == CORE_DOCKER / "edge"
    assert docker.CONTROL_PLANE_DOCKERFILE == CORE_DOCKER / "control-plane/Dockerfile"
    for constant in (
        docker.EDGE_DOCKERFILE,
        docker.EDGE_MODULE_PROBE_CONF,
        docker.CONTROL_PLANE_DOCKERFILE,
        docker.CONTROL_PLANE_DOCKERIGNORE,
    ):
        assert constant.is_file(), constant


def test_no_tracked_file_names_the_old_top_level_docker_directory():
    """The build inputs live in the package; nothing may point at `docker/`.

    A path like `docker/edge` works in a checkout and fails on an installed
    controller. Python reads the constants; the justfile, shell scripts and
    release workflow spell the path under `src/blitzecdn/`.
    """
    import shutil
    import subprocess

    git = shutil.which("git")
    assert git, "this asks git which files are tracked"
    tracked = subprocess.run(  # noqa: S603 - fixed argv built here
        [git, "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    offenders = [
        f"{name}:{number}"
        for name in tracked
        for path in ((REPO_ROOT / name),)
        # This module holds the needles, so it is the one file allowed to spell the old
        # form. Prose elsewhere is not exempt.
        if path.is_file()
        and not name.startswith("src/blitzecdn/assets/docker/")
        and path != Path(__file__).resolve()
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        )
        for stale in ("docker/edge", "docker/control-plane")
        if stale in line and f"src/blitzecdn/assets/{stale}" not in line
    ]
    assert offenders == [], (
        "these name the pre-move `docker/` directory, which only a checkout "
        f"has: {offenders}"
    )
