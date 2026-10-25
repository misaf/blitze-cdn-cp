from __future__ import annotations

from pathlib import Path

from paths import CORE_DOCKER, REPO_ROOT


def test_the_published_docker_paths_are_the_ones_in_the_checkout():
    """`blitzecdn.assets.docker` and the checkout tree are the same directory.

    The module resolves through `importlib.resources`, which in a checkout
    with an editable install answers the checkout — so every other suite can
    read the constants and still be reading the files under review. The day
    that stops being true is the day the packaging moved and nothing said so.
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
    """The build inputs moved into the wheel; nothing may point back.

    A textual guard, because that is the shape of the regression: a new script
    or workflow copying the old `docker/edge` incantation would work in a
    checkout and fail on an installed controller, which is the exact failure
    mode the move removed. Python callers should read the constants; the
    justfile, the two root-run shell scripts and the release workflow cannot
    import anything, so they spell the path under `src/blitzecdn/` and this
    only refuses the pre-move form.
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
        # This module is the one file that has to spell the pre-move form:
        # the needles below are it. Prose elsewhere is deliberately *not*
        # exempt — a comment telling somebody to build `docker/edge` sends
        # them somewhere that no longer exists just as surely as a command
        # would.
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
