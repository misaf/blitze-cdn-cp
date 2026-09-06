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

import pytest
from paths import REPO_ROOT

PROJECT_DIR = REPO_ROOT
HTTP3_HARNESS = PROJECT_DIR / "tests/http3-edge-integration.sh"
INSTALL_HARNESS = PROJECT_DIR / "tests/container-install.sh"
HTTP3_PLAYBOOK = PROJECT_DIR / "tests/integration/http3-edge.yml"
# Both harnesses start a privileged systemd host and install a container engine
# inside it, so both meet the nested-overlay limit below in the same way.
NESTED_ENGINE_HARNESSES = (HTTP3_HARNESS, INSTALL_HARNESS)


def _commands(harness=HTTP3_HARNESS) -> str:
    """A harness with its comments removed."""
    return "\n".join(
        line
        for line in harness.read_text(encoding="utf-8").splitlines()
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


@pytest.mark.parametrize("harness", NESTED_ENGINE_HARNESSES, ids=lambda path: path.stem)
def test_the_nested_engine_keeps_its_images_off_the_outer_overlay(harness):
    """A container engine cannot run out of a container's own rootfs.

    The engine inside the host container assembles every image as an overlay
    mount whose upper and lower directories are under /var/lib/docker and
    /var/lib/containerd — which, without a volume, are the outer container's
    rootfs, itself overlay. The mount is refused, and what the engine reports
    is `invalid argument` about a mount path, naming neither an image nor a
    layer nor the reason. Both jobs spent weeks red on it.

    A shape assertion for the same reason as the rest of this module: nothing a
    pull request runs starts either harness, so the gate that catches a dropped
    `-v` has to be this one.
    """
    commands = _commands(harness)
    # The privileged one: the HTTP/3 harness also starts an unprivileged origin
    # container, which runs no engine and needs none of this.
    hosts = [
        block
        for block in re.findall(r"docker run .*?\n\n", commands, re.DOTALL)
        if "--privileged" in block
    ]
    assert len(hosts) == 1, (
        f"{harness.name} starts {len(hosts)} privileged host containers; this "
        "assertion knows how to check exactly one"
    )
    for data_root in ("/var/lib/docker", "/var/lib/containerd"):
        assert f"-v {data_root} " in hosts[0], (
            f"{harness.name} gives the nested engine no volume for "
            f"{data_root}, so its overlay mounts land on the outer "
            "container's own overlay rootfs and are refused"
        )


@pytest.mark.parametrize("harness", NESTED_ENGINE_HARNESSES, ids=lambda path: path.stem)
def test_the_nested_engines_volumes_are_removed_with_their_host(harness):
    """Anonymous volumes outlive the container unless the removal says so.

    They hold a whole engine's images. A runner that keeps them keeps
    gigabytes per run, and neither harness would notice.
    """
    commands = _commands(harness)
    assert re.search(r"docker rm -f -v ", commands), (
        f"{harness.name} removes its host container without -v, leaving the "
        "nested engine's images behind on the machine that ran it"
    )


def test_the_edge_image_is_built_with_the_modules_the_capabilities_declare():
    """An image built with no modules fails four steps past the cause.

    `ENABLED_MODULES` and its two siblings default to empty in the Dockerfile,
    so a plain `docker build` produces an image carrying only what the base
    image ships. The converge then reaches `modules-invariant.yml` and refuses
    by naming a capability and an image digest — an accurate message about the
    wrong half of the problem, since it is the build that dropped them.

    The set is composed, never written down: `blitzecdn edge image spec` reads
    the capabilities installed in the workspace, and the workflow that
    publishes the edge runtime builds from the same command. A harness that
    hardcoded a module list would pass while the published image carried
    something else.
    """
    commands = _commands()
    spec = re.search(r"uv run .*blitzecdn edge image spec", commands)
    assert spec, (
        "the harness no longer resolves the edge image's module set from the "
        "installed capabilities"
    )

    build = re.search(r'docker build (.+?)--tag "\$\{EDGE_TAG\}"', commands, re.DOTALL)
    assert build, "the harness no longer builds the edge image"
    assert "${module_args[@]}" in build.group(1), (
        "the edge image is built without the resolved module arguments, so it "
        "carries only what the base image ships"
    )


def test_the_harness_gives_the_capability_slot_the_roles_it_requires():
    """Two variables, one letter apart, and only one of them is the role's.

    `blitzecdn_capability_roles` is what a control plane composes from its
    installed plugins and passes in as an extra-var.
    `blitzecdn_capabilities_roles` is the `blitzecdn_capabilities` role's own
    required parameter, set per slot because the real play stands the role up
    twice with different lists. `ansible/playbooks/edge.yml` translates the
    first into the second at every invocation; a play that sets only the first
    hands the role nothing and fails its argument spec, four roles into a
    converge that has already provisioned a host.
    """
    playbook = HTTP3_PLAYBOOK.read_text(encoding="utf-8")
    invocations = re.findall(
        r"- role: blitzecdn_capabilities\n(.*?)(?=\n    - role: |\Z)",
        playbook,
        re.DOTALL,
    )
    assert invocations, "the harness no longer runs the capability slot"
    for body in invocations:
        assert "blitzecdn_capabilities_roles:" in body, (
            "the harness stands up blitzecdn_capabilities without the roles "
            "parameter it requires; setting blitzecdn_capability_roles as a "
            "play variable is not the same thing"
        )
