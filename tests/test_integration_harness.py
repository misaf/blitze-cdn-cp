"""Structural assertions about the container-based integration harnesses.

`tests/http3-edge-integration.sh` provisions a real edge inside a privileged
systemd container. Nothing else in the suite runs it: it needs Docker, several
minutes and a build of the edge image, so it lives in its own CI job. That
leaves a gap these tests fill — every gate a pull request actually runs
(pytest, ruff, yamllint, ansible-lint, shellcheck) was green while the harness
could not complete a single converge, because it saved the edge images to a
tarball, mounted them into the edge host, and never loaded them into that
host's engine.

So the assertions here are deliberately about *shape*, not behaviour: each one
names a step whose absence turns the integration job into a failure nobody can
reproduce from a local `just check`. They read the script's executable lines
only. Prose is where the harness explains itself, and a comment that mentions
`docker load` is not a `docker load`.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
HTTP3_HARNESS = PROJECT_DIR / "tests/http3-edge-integration.sh"


def _commands() -> str:
    """The harness with its comments removed."""
    return "\n".join(
        line
        for line in HTTP3_HARNESS.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_every_saved_edge_image_reaches_the_edge_hosts_engine():
    """Saving an image is not giving it to the host that has to run it.

    The edge host runs its own Docker Engine, and `http3-edge.yml` sets
    `blitzecdn_edge_stack_image_pull: false` precisely so the test proves the
    image this repository builds rather than whatever the registry publishes.
    An image that is saved but never loaded therefore fails the converge at
    `Require the requested image to be present` — which is exactly what
    happened, undetected, because no gate runs this script.
    """
    commands = _commands()
    saved = re.search(r"docker save (.+?) -o", commands)
    assert saved, "the harness no longer saves the edge images"

    tags = set(re.findall(r"\$\{(EDGE_TAG(?:_[A-Z]+)?)\}", saved.group(1)))
    assert tags, "no image tags are saved"

    assert "docker load" in commands, (
        "the harness saves the edge images but never loads them into the edge "
        "host's engine, so the first converge cannot find the image it is "
        "forbidden from pulling"
    )

    # Loaded is not the same as arrived: the load is one command for the whole
    # tarball, so the check that every tag is present has to name every tag.
    verified = re.search(
        r"for tag in (.+?); do\n\s*in_edge \"docker image inspect \$\{tag\}",
        commands,
    )
    assert verified, "nothing confirms the images arrived on the edge host"
    assert (
        set(re.findall(r"\$\{(EDGE_TAG(?:_[A-Z]+)?)\}", verified.group(1))) == tags
    ), "the harness saves a set of image tags and verifies a different one"


def test_the_engine_is_installed_before_the_images_are_loaded():
    """`docker load` has no engine to load into until blitzecdn_docker has run.

    The main converge installs the engine and then requires the image a few
    tasks later, which is too late — so the engine is installed by its own
    play first. Ordering is the whole point of that play existing.
    """
    commands = _commands()
    engine_play = commands.index("tests/integration/docker-engine.yml")
    load = commands.index("docker load")
    fresh_converge = commands.index('say "Converging a fresh Docker edge"')

    assert engine_play < load < fresh_converge, (
        "the engine must be installed, then the images loaded, then the fresh "
        "edge converged"
    )
    assert (PROJECT_DIR / "tests/integration/docker-engine.yml").is_file()


def test_the_harness_still_proves_a_repeated_converge_changes_nothing():
    """Idempotency is the property most easily lost and least easily noticed."""
    assert "changed=0" in _commands()
