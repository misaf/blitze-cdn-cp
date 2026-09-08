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

import os
import re
import shlex

import pytest
import yaml
from paths import CORE_ANSIBLE, REPO_ROOT

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


def test_the_harness_certificates_live_where_the_runtime_can_read_them():
    """A path the edge container has no mount for is a file nginx cannot open.

    `certificate_mode: existing` is the one mode validate.yml does not check
    the shape of, so an unreachable path is not refused by the role — it is
    refused by nginx, inside a config-test container, after the whole tree has
    been rendered and every capability role has run.

    The reachable directory is the runtime contract's `paths.tls`, which
    compose.yml.j2 mounts read-only into the container at the same path, and
    it is also where the script generates the certificate it later asserts
    survives a teardown. This checks the play against the script rather than
    against a constant, so the two cannot drift apart again.
    """
    playbook = HTTP3_PLAYBOOK.read_text(encoding="utf-8")
    written = set(re.findall(r"(/etc/blitzecdn/tls/[\w.-]+)", _commands(HTTP3_HARNESS)))
    assert written, "the harness no longer generates a certificate to serve"

    declared = re.findall(r"certificate(?:_key)?_path: (\S+)", playbook)
    assert declared, "the harness play no longer names a certificate"
    for path in declared:
        assert path in written, (
            f"the play serves {path}, which the harness never creates and the "
            "edge container has no mount for; only paths under the runtime's "
            f"TLS directory reach nginx, and the script writes {sorted(written)}"
        )


def test_the_idempotency_check_reports_what_failed_to_settle():
    """`changed=1` is a count, and the harness costs five minutes to reach it.

    A guard for a diagnostic rather than for behaviour, which this module has
    earned the right to hold: every failure in this harness is expensive to
    reproduce, and the one that motivated `--diff` was a single file task
    flipping one attribute of one path with nothing in the log to say which.
    """
    commands = _commands()
    converge = re.search(r"converged=\$\(converge([^)]*)\)", commands)
    assert converge, "the harness no longer captures a repeated converge"
    assert "--diff" in converge.group(1), (
        "the idempotency check runs without --diff, so a converge that fails "
        "to settle reports a count and not the attribute that moved"
    )


def test_the_upgrade_moves_to_different_bytes():
    """Retagging is not an upgrade, and the role is right to ignore it.

    blitzecdn_edge_stack resolves a reference to a digest and pins the Compose
    file to that, so a second tag for bytes already running leaves the file
    identical and recreates nothing — deliberately, because a tag resolved
    twice must not be able to move a fleet. The harness used to retag and then
    assert the container had been replaced, which asserted the opposite of a
    documented design decision and failed exactly as the role intended.

    So the upgrade target has to be built. What is in it does not matter — a
    label is enough — but it has to be its own image.
    """
    commands = _commands()
    assert not re.search(r"docker tag .*EDGE_TAG_NEXT", commands), (
        "the upgrade target is a second tag of the running image, so its "
        "digest is the one already deployed and nothing will be recreated"
    )
    built = re.search(r"docker build [^\n]*--tag \"\$\{EDGE_TAG_NEXT\}\"", commands)
    assert built, "the upgrade target is no longer built as its own image"


#: `blitzecdn` at the start of a command — after a pipeline or list operator, a
#: subshell, or an opening quote. Deliberately not after a word: the harnesses
#: quote the CLI in their `fail` messages ("unexpected blitzecdn host account"),
#: and prose that names a command is not a call to it.
_INVOCATION = re.compile(r"""(?:^|[;&|(]|&&|\|\||['"])\s*blitzecdn\s+([^'"|;>)]*)""")


def _cli_invocations(harness):
    """Every `blitzecdn ...` the harness actually runs, as argument lists."""
    for line in _commands(harness).splitlines():
        for match in _INVOCATION.finditer(line):
            try:
                yield shlex.split(match.group(1))
            except ValueError:  # an unbalanced quote from the surrounding shell
                yield match.group(1).split()


#: The pinned CLI surface: `<distribution>\t command \t <path> \t <spec>`. Read
#: rather than introspected, because the harnesses run an installation with
#: every optional wheel present and `just test-core-only` runs with none of
#: them. Resolving against the live app would make this test assert that
#: `backup create` does not exist in exactly the configuration where the
#: installer proves that it does. `contract/test_frozen` is what keeps this
#: file honest about the app.
FROZEN_CLI = PROJECT_DIR / "tests/contract/frozen/cli.txt"


def _published_commands():
    """Every published command path, mapped to the long options it declares."""
    published = {}
    for line in FROZEN_CLI.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) < 3 or fields[1] != "command":
            continue
        path = fields[2].removeprefix("blitzecdn ")
        spec = fields[3] if len(fields) > 3 else ""
        published[path] = set(re.findall(r"--[\w-]+", spec))
    return published


def _resolve(tokens, published):
    """Match the longest published command path the tokens begin with."""
    for length in range(min(len(tokens), 3), 0, -1):
        path = " ".join(tokens[:length])
        if path in published:
            return path, length
    return None, 0


@pytest.mark.parametrize("harness", NESTED_ENGINE_HARNESSES, ids=lambda p: p.name)
def test_the_harnesses_call_the_cli_this_repository_actually_ships(harness):
    """The shell harnesses are the only callers no gate type-checks.

    `record add --value ... --proxied` survived the removal of `--proxied` for
    three CI runs. Every Python caller was updated with the model — a record is
    proxied exactly when it names a site — but a flag inside a single-quoted
    string in a shell script is invisible to ruff, to mypy and to pytest, and
    the integration job that would have caught it was already red for an
    unrelated reason. So resolve each invocation against the real Typer app:
    the command must exist, and every long option must be one it declares.
    """
    published = _published_commands()
    for tokens in _cli_invocations(harness):
        # `blitzecdn --version` addresses the root, which publishes no command
        # line of its own for this to resolve against.
        if not tokens or tokens[0].startswith("-"):
            continue
        path, consumed = _resolve(tokens, published)
        assert path, (
            f"{harness.name} runs `blitzecdn {' '.join(tokens)}`, "
            f"which is not a command any distribution publishes"
        )
        for token in tokens[consumed:]:
            if not token.startswith("--"):
                continue
            name = token.split("=", 1)[0]
            assert name in published[path], (
                f"{harness.name} passes {name} to `{path}`, which declares "
                f"only {sorted(published[path])}"
            )


def test_the_install_harness_reads_the_logs_where_the_host_keeps_them():
    """A diagnostic that prints nothing is worse than no diagnostic.

    The drift check dumps the last Ansible log when the fleet reports drift,
    and the runner invokes `--check --diff`, so that log is exactly where the
    disagreement is written down. It looked under `/opt/blitzecdn/.state/logs`
    — which is where the control plane sees its state from *inside* the CLI
    container. The host path behind that bind mount is what a `docker exec`
    into the systemd host can read, and the one failure the diagnostic exists
    for printed `No such file or directory` instead of the diff.
    """
    state_dir = yaml.safe_load(
        (
            REPO_ROOT
            / "src/blitzecdn/ansible/roles/blitzecdn_controlplane/defaults/main.yml"
        ).read_text(encoding="utf-8")
    )["blitzecdn_controlplane_state_dir"]
    commands = _commands(INSTALL_HARNESS)

    assert f"{state_dir}/logs" in commands, (
        f"the harness does not read the host's log directory ({state_dir}/logs)"
    )
    assert "/opt/blitzecdn/.state/logs" not in commands, (
        "the harness reads the container's view of the state directory, which "
        "is not a path the host it runs `docker exec` against can see"
    )


def test_the_drift_diagnostic_names_the_files_the_stack_renders_itself_from():
    """A check-mode diff is half a comparison.

    It says which line the fleet disagrees about; what the converge actually
    settled on lives in the compose file and the recorded image, and neither is
    in any log. The harness therefore prints both when drift is reported — and
    it spells the paths, so this reads them out of the roles that own them.
    They were guessed once, and a diagnostic that cats a path nothing writes is
    the failure this whole test exists for.
    """
    edge = yaml.safe_load(
        (CORE_ANSIBLE / "roles/blitzecdn_edge/defaults/main.yml").read_text(
            encoding="utf-8"
        )
    )
    stack = yaml.safe_load(
        (CORE_ANSIBLE / "roles/blitzecdn_edge_stack/defaults/main.yml").read_text(
            encoding="utf-8"
        )
    )
    compose = (
        stack["blitzecdn_edge_stack_compose_file"]
        .strip()
        .replace(
            "{{ blitzecdn_edge_runtime.paths.state }}",
            edge["blitzecdn_edge_runtime"]["paths"]["state"],
        )
    )
    commands = _commands(INSTALL_HARNESS)

    for path in (compose, stack["blitzecdn_edge_stack_deployed_image_file"]):
        assert path in commands, (
            f"the drift diagnostic does not print {path}, which is one of the "
            "two files the disagreement is between"
        )


#: What the CLI container can see. The `blitzecdn` wrapper runs the CLI inside
#: the control plane's own container, so a path is addressable only if it falls
#: under one of that service's bind mounts — and the host and the container do
#: not agree on the names. The state directory in particular is
#: /var/lib/blitzecdn to the host and /opt/blitzecdn/.state to the CLI.
#: A mount is written as `<host variable> ~ '<suffix>:<target>[:ro]'`, so the
#: container's name for it is the second colon-separated field of the literal.
_CLI_MOUNT = re.compile(r"~\s*'([^']+)'")


def _cli_visible_paths():
    """The container-side path of every mount the CLI service declares."""
    template = (
        CORE_ANSIBLE / "roles/blitzecdn_controlplane/templates/compose.yml.j2"
    ).read_text(encoding="utf-8")
    service = template.split("blitzecdn-cli:", 1)[1].split("entrypoint:", 1)[0]
    targets = set()
    for match in _CLI_MOUNT.finditer(service):
        fields = match.group(1).split(":")
        if len(fields) >= 2 and fields[1].startswith("/"):
            targets.add(fields[1])
    return targets


@pytest.mark.parametrize("harness", NESTED_ENGINE_HARNESSES, ids=lambda p: p.name)
def test_the_harnesses_name_paths_the_cli_can_reach_from_its_container(harness):
    """A host path handed to the CLI is a path the CLI cannot open.

    The harness runs on the host and the CLI runs in a container, and the two
    have different names for the same directory. Writing a backup to the state
    directory by its container name and then restoring it by its host name is
    one file and two spellings, and it failed with `does not exist` about a
    path that plainly did exist — from the host, which is where the person
    reading the failure is standing.

    Every such path has been wrong at least once: the Ansible log, the compose
    file, this archive. So the mounts are read from the service that declares
    them rather than restated here.
    """
    visible = _cli_visible_paths()
    assert "/opt/blitzecdn/.state" in visible, (
        "the CLI service no longer mounts the state directory; this test is "
        "reading the wrong service"
    )
    for tokens in _cli_invocations(harness):
        for token in tokens:
            if not token.startswith("/"):
                continue
            assert any(
                token == mount or token.startswith(mount.rstrip("/") + "/")
                for mount in visible
            ), (
                f"{harness.name} passes {token} to the CLI, which runs inside "
                f"the control plane's container and can only see {sorted(visible)}"
            )


def test_the_harness_serves_an_http01_challenge_through_the_shipped_playbook():
    """The only flow where the host writes and the container reads.

    Nginx serves the ACME webroot from a read-only bind mount as the image's
    uid, which matches neither a host owner nor a host group. A mode that
    admits only a host account answers 403 for a file that is plainly on disk,
    and ACME reports that as a failed challenge rather than as a permission —
    which is how it stayed true of every edge while every gate was green. No
    harness issued a certificate, so nothing ever fetched a token.

    Through the shipped playbook, not by writing the token here: the writer and
    the server agreeing is the whole property, and a harness that published the
    file itself would pass while the real writer put it somewhere unreadable.
    """
    commands = _commands(HTTP3_HARNESS)

    playbook = (
        "packages/blitzecdn-certificates/src/blitzecdn_certificates"
        "/ansible/playbooks/acme-challenge.yml"
    )
    assert (REPO_ROOT / playbook).is_file(), "the challenge playbook moved"
    assert playbook in commands, (
        "the harness no longer runs the shipped challenge playbook, so it "
        "proves nothing about the writer the control plane actually uses"
    )

    # Published and withdrawn. A token that outlives its order is a file the
    # next validation could be answered with.
    #
    # The action reaches the playbook through a helper, so the helper's name is
    # found rather than assumed: asserting on a literal `...action=present`
    # would fail the day someone factors the two calls together, which is not
    # a regression in anything.
    helper = re.search(
        r"^(\w+)\(\) \{(?:(?!\n\}).)*blitzecdn_acme_action=",
        commands,
        re.MULTILINE | re.DOTALL,
    )
    assert helper, "no helper in the harness drives the challenge playbook"
    for action in ("present", "absent"):
        assert f"{helper.group(1)} {action}" in commands, (
            f"the harness never runs the challenge playbook with {action}"
        )

    # Over port 80, which is where a CA looks, and asserting on the body rather
    # than only the status: a 200 carrying the wrong bytes fails validation
    # just as surely, and is what a stale token looks like.
    assert "site-one.test:80:${edge_ip}" in commands, (
        "the challenge is not fetched over the port ACME uses"
    )
    assert "ACME_VALIDATION" in commands.split("body=")[1][:200], (
        "the harness does not compare the served body with the validation value"
    )


def test_the_install_harness_issues_against_a_ca_that_really_validates():
    """The control plane's half of ACME, and the ways it could pass hollow.

    Issuance is the one flow that leaves the control plane and comes back:
    certbot, the manual hooks, the playbook those hooks drive, the chain the
    store validates. Every part of it was unexercised, so the assertions here
    are less about the happy path than about the shortcuts that would make the
    stage green while proving nothing.

    The load-bearing one is `httpPort`. Pebble validates HTTP-01 against port
    5002 by default, where its own challenge server answers; pointed there it
    would issue a certificate without the edge being asked for anything, and
    every assertion downstream would still hold.
    """
    commands = _commands(INSTALL_HARNESS)
    text = INSTALL_HARNESS.read_text(encoding="utf-8")

    # A real CA, pinned. `:latest` is a harness that fails on a commit that did
    # not touch it, on somebody else's release schedule.
    assert re.search(r"ghcr\.io/letsencrypt/pebble:\d+\.\d+\.\d+", commands), (
        "the ACME server is not a pinned Pebble image"
    )
    assert "letsencrypt/pebble:latest" not in commands

    # Validation reaches the edge's own listener.
    assert '"httpPort": 80' in commands, (
        "Pebble is not pointed at the edge's HTTP listener, so issuance would "
        "succeed without the edge serving the challenge"
    )
    # The escape hatch that would make every challenge pass unasked.
    assert "PEBBLE_VA_ALWAYS_VALID" not in text, (
        "the harness tells Pebble to accept challenges without validating them"
    )

    # The configuration change has to actually reach the running process. On
    # this stage's first run `up --detach` found both services up-to-date and
    # left them alone, so issuance ran against the previous configuration —
    # which meant the real Let's Encrypt — and the only symptom was a rejected
    # email address.
    assert "--force-recreate" in commands, (
        "the control plane is not replaced after being reconfigured, so "
        "issuance can run against the CA it was already pointed at"
    )
    assert "StartedAt" in commands, "nothing checks that the restart replaced anything"

    # Through the product's own issuance path, not by driving certbot directly.
    assert "/certificate/request" in commands, (
        "the harness does not ask the control plane to issue the certificate"
    )

    # certbot reaches Pebble through the committed wrapper, which has to be
    # both present and executable: the control-plane image copies it in, and a
    # file the image cannot execute fails as `certbot is not available`.
    wrapper = REPO_ROOT / "tests/integration/certbot-pebble"
    assert wrapper.is_file(), "the test certbot wrapper is missing"
    assert os.access(wrapper, os.X_OK), "the test certbot wrapper is not executable"
    # Its executable lines, not its text: the comment above the `exec` explains
    # why `--server` is where the CA is named, and a comment is not a flag.
    executed = "\n".join(
        line
        for line in wrapper.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "--server" in executed, (
        "the wrapper does not redirect certbot to the test CA, so issuance "
        "would be attempted against the real one"
    )
    assert str(wrapper.relative_to(REPO_ROOT)) in commands, (
        "the harness does not point the control plane at the wrapper"
    )

    # The issued chain is verified against the root it came from, and for the
    # name it was issued for. Without both, the last assertion degrades to
    # "something answered on 443".
    assert "-CAfile" in commands and "-verify_return_error" in commands, (
        "the harness does not verify the served chain against the ACME root"
    )
    assert "-verify_hostname" in commands, (
        "the harness does not check the served certificate covers the name"
    )


def test_the_install_harness_proves_a_renewal_changed_something():
    """Renewal's failure mode is succeeding without doing anything.

    `cert renew` reports skipped sites and exits zero, which is correct for a
    scheduled sweep and useless as a test: a run that renewed nothing, or one
    that renewed into the store and never reached the edge, looks exactly like
    a run that worked. The only assertion that separates them is that the
    certificate the edge presents is not the one it presented before.

    Read from the wire, twice, and compared — not from `cert list`, which is
    the control plane agreeing with itself.
    """
    commands = _commands(INSTALL_HARNESS)

    # The renewal's own invocation, not the whole file: `--force` also appears
    # in the `--force-recreate` that restarts the control plane, and an
    # assertion that reads it there passes for an unforced renewal.
    renew = re.search(r"blitzecdn cert renew[^\n\"]*", commands)
    assert renew, "the harness never renews anything"
    invocation = renew.group()

    # Nothing is near expiry a minute after issuance, so without --force the
    # sweep correctly does nothing at all.
    assert "--force" in invocation, (
        "the renewal cannot renew: no certificate is due, so an unforced sweep "
        "skips every site and still exits zero"
    )
    assert "--deploy" in invocation, (
        "the renewal never reaches an edge, so it proves only that the store "
        "was updated"
    )

    # The before/after comparison itself.
    assert re.search(r"\$\{renewed_from\} != \"\$\{renewed_to\}\"", commands), (
        "the harness does not compare the certificate served before the "
        "renewal with the one served after it"
    )
    for capture in (
        "renewed_from=$(served_certificate",
        "renewed_to=$(served_certificate",
    ):
        assert capture in commands, f"{capture} is not read from the wire"

    # Preflight is the gate renewal cannot bypass, and the only place it runs
    # for real: the initial request skips it.
    assert "cert preflight" in commands, (
        "nothing exercises the preflight that every renewal has to pass"
    )

    # The DNS server both the CA and preflight depend on is asked whether it
    # is answering, rather than assumed to be. `docker run -d` reports success
    # for a container that starts and dies — an unknown flag looks exactly like
    # a clean start — and the first symptom is a challenge that fails with no
    # reason given, several minutes later.
    assert re.search(r"dig .*@127\.0\.0\.1", commands), (
        "nothing checks that the challenge DNS server actually resolves"
    )

    # Otherwise Pebble reuses an authorization about half the time, and whether
    # the renewal revalidates over HTTP-01 is decided per run by a coin toss.
    assert "PEBBLE_AUTHZREUSE=0" in commands, (
        "authorization reuse is left at its default, so the renewal's coverage "
        "varies from run to run"
    )
