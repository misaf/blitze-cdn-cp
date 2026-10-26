"""Structural assertions about the container-based integration harnesses.

`tests/http3-edge-integration.sh` provisions a real edge in a privileged
systemd container. It needs Docker, minutes and an edge image build, so only a
dedicated CI job runs it, and no pull-request gate does.

So these assert shape, not behaviour: each names a step whose absence breaks
the integration job in a way `just check` cannot reproduce. They read
executable lines only; a comment mentioning `docker load` is not one.
"""

from __future__ import annotations

import os
import re
import shlex

import http3_release
import pytest
import yaml
from support.paths import CORE_ANSIBLE, REPO_ROOT

from blitzecdn.core.domain.configuration import ConfigurationBundle

PROJECT_DIR = REPO_ROOT
HTTP3_HARNESS = PROJECT_DIR / "tests/http3-edge-integration.sh"
INSTALL_HARNESS = PROJECT_DIR / "tests/container-install.sh"
# The desired state the harness renders a release from, and what its controller
# passes beside the release. The edge itself is converged by the shipped play.
HTTP3_STATE = PROJECT_DIR / "tests/integration/http3-edge/release.yml"
HTTP3_RUNNER = PROJECT_DIR / "tests/integration/http3-edge/runner.yml"
EDGE_PLAYBOOK = "src/blitzecdn/assets/ansible/playbooks/edge.yml"
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

    The edge host runs its own engine and the harness's runner disables pulls, so a
    saved but unloaded image fails at `Require the requested image to be present`.
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

    So the engine is installed by its own play, before the main converge.
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

    Without volumes, /var/lib/docker and /var/lib/containerd are on the outer
    overlay, and the nested overlay mount fails with an unhelpful
    `invalid argument`.
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

    They hold a whole engine's images.
    """
    commands = _commands(harness)
    assert re.search(r"docker rm -f -v ", commands), (
        f"{harness.name} removes its host container without -v, leaving the "
        "nested engine's images behind on the machine that ran it"
    )


def test_the_edge_image_is_built_with_the_modules_the_capabilities_declare():
    """An image built with no modules fails four steps past the cause.

    `ENABLED_MODULES` and its siblings default to empty, and the converge then
    fails in `modules-invariant.yml`. The set comes from `blitzecdn edge image
    spec`, the same command the publishing workflow uses.
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


def test_the_harness_converges_the_shipped_edge_play_from_a_release():
    """A test play is a copy, and a copy drifts.

    The harness once ran its own play, which predated content-addressed
    releases: it never selected a candidate tree, so the stack mounted
    configuration paths nothing wrote any more, and the job was red on every
    commit. The edge is now converged by `edge.yml` itself, from a release
    the control plane's renderer produced, and committed only after the edge
    answers with that release — the rollout's own order.
    """
    commands = _commands()
    assert (REPO_ROOT / EDGE_PLAYBOOK).is_file(), "the edge play moved"
    assert EDGE_PLAYBOOK in commands, "the harness does not run the shipped edge play"
    assert not list((REPO_ROOT / "tests/integration").glob("http3-edge.yml")), (
        "a harness-only edge play is back beside the shipped one"
    )
    assert "tests/http3_release.py" in commands, (
        "the harness no longer renders the release it deploys"
    )

    deploy = re.search(r"^deploy\(\) \{\n(.*?)\n\}", commands, re.MULTILINE | re.DOTALL)
    assert deploy, "the harness has no rollout helper"
    body = deploy.group(1)
    verify = body.index("/.blitzecdn/release")
    commit = body.index("--tags commit")
    assert body.index("converge") < verify < commit, (
        "the harness commits a release without first checking the edge serves it"
    )


def test_the_harness_capability_slot_is_the_one_the_renderer_loads():
    """The renderer loads the defaults of the roles the play will run.

    So the runner names the slot once, as `blitzecdn_capability_roles` — the
    extra-var `edge.yml` translates into the role's own parameter — and the
    release helper reads the list from there rather than from a second copy.
    """
    runner = yaml.safe_load(HTTP3_RUNNER.read_text(encoding="utf-8"))
    roles = runner["blitzecdn_capability_roles"]
    assert roles, "the harness converges no capability"
    assert "blitzecdn_capabilities_roles" not in runner, (
        "the runner sets the role's own parameter, which edge.yml derives"
    )


def test_the_harness_release_renders_and_is_content_addressed(tmp_path):
    """What the harness stages is a real bundle, and new state is a new release.

    The image upgrade and the rollback are each a new digest: a sealed
    candidate reuses the image it was validated under.
    """
    arguments = [str(HTTP3_STATE), str(HTTP3_RUNNER)]
    edge = "/harness/release"
    first = tmp_path / "first"
    assert http3_release.main([*arguments, str(first), edge]) == 0
    second = tmp_path / "second"
    upgrade = "blitzecdn_edge_image=blitzecdn-edge:integration-next"
    assert http3_release.main([*arguments, str(second), edge, upgrade]) == 0

    released = yaml.safe_load((first / http3_release.EXECUTION_DOCUMENT).read_text())
    upgraded = yaml.safe_load((second / http3_release.EXECUTION_DOCUMENT).read_text())
    assert released["blitzecdn_release_digest"] != upgraded["blitzecdn_release_digest"]
    assert released["blitzecdn_config_release_bundle"] == (
        f"{edge}/{http3_release.BUNDLE}"
    )

    bundle = ConfigurationBundle.decode((first / http3_release.BUNDLE).read_text())
    candidate = f"/etc/blitzecdn/releases/{released['blitzecdn_release_digest']}"
    assert set(bundle.links) == {
        "nginx/sites-enabled/blitzecdn-default.conf",
        "nginx/sites-enabled/site-one-test.conf",
        "nginx/sites-enabled/site-two-test.conf",
    }
    assert all(target.startswith(candidate) for target in bundle.links.values())
    assert "njs/under-attack.js" in bundle.files

    origin = re.search(r"python -m http\.server (\d+)", _commands())
    assert origin, "the harness has no HTTP origin"
    for site in released["blitzecdn_nginx_sites"]:
        rendered = bundle.files[f"nginx/sites-available/{site['name']}.conf"].decode()
        assert f"http://{site['origin_host']}:{origin[1]};" in rendered, (
            "the rendered upstream does not reach the harness origin's port"
        )


def test_the_harness_certificates_live_where_the_runtime_can_read_them():
    """A path the edge container has no mount for is a file nginx cannot open.

    validate.yml does not check `certificate_mode: existing` paths, so only nginx
    would refuse them, after everything has run. The readable directory is the
    runtime contract's `paths.tls`; this checks the desired state against the script.
    """
    playbook = HTTP3_STATE.read_text(encoding="utf-8")
    written = set(re.findall(r"(/etc/blitzecdn/tls/[\w.-]+)", _commands(HTTP3_HARNESS)))
    assert written, "the harness no longer generates a certificate to serve"

    declared = re.findall(r"certificate(?:_key)?_path: (\S+)", playbook)
    assert declared, "the harness's desired state no longer names a certificate"
    for path in declared:
        assert path in written, (
            f"the play serves {path}, which the harness never creates and the "
            "edge container has no mount for; only paths under the runtime's "
            f"TLS directory reach nginx, and the script writes {sorted(written)}"
        )


def test_the_idempotency_check_reports_what_failed_to_settle():
    """`changed=1` is a count; `--diff` says which task flipped."""
    commands = _commands()
    converge = re.search(r"converged=\$\(converge([^)]*)\)", commands)
    assert converge, "the harness no longer captures a repeated converge"
    assert "--diff" in converge.group(1), (
        "the idempotency check runs without --diff, so a converge that fails "
        "to settle reports a count and not the attribute that moved"
    )


def test_the_upgrade_moves_to_different_bytes():
    """Retagging is not an upgrade, and the role is right to ignore it.

    blitzecdn_edge_stack pins the Compose file to a digest, so a second tag for
    the same bytes recreates nothing. The upgrade target must be its own image; a
    label is enough.
    """
    commands = _commands()
    assert not re.search(r"docker tag .*EDGE_TAG_NEXT", commands), (
        "the upgrade target is a second tag of the running image, so its "
        "digest is the one already deployed and nothing will be recreated"
    )
    built = re.search(r"docker build [^\n]*--tag \"\$\{EDGE_TAG_NEXT\}\"", commands)
    assert built, "the upgrade target is no longer built as its own image"


#: `blitzecdn` at the start of a command: after a pipeline or list operator, a
#: subshell, or an opening quote. Not after a word, since `fail` messages name
#: the CLI in prose.
_INVOCATION = re.compile(r"""(?:^|[;&|(]|&&|\|\||['"])\s*blitzecdn\s+([^'"|;>)]*)""")


def _cli_invocations(harness):
    """Every `blitzecdn ...` the harness actually runs, as argument lists."""
    for line in _commands(harness).splitlines():
        for match in _INVOCATION.finditer(line):
            try:
                yield shlex.split(match.group(1))
            except ValueError:  # an unbalanced quote from the surrounding shell
                yield match.group(1).split()


#: The pinned CLI surface: `<distribution>\t command \t <path> \t <spec>`.
#: `contract/test_frozen` keeps this file honest about the app.
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

    Each invocation is resolved against the published CLI: the command must exist
    and every long option must be one it declares.
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

    On drift the harness dumps the last Ansible log, which must be read at the
    host path behind the bind mount, not the CLI container's path.
    """
    state_dir = yaml.safe_load(
        (
            REPO_ROOT
            / "src/blitzecdn/assets/ansible"
            / "roles/blitzecdn_controlplane/defaults/main.yml"
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

    What the converge settled on lives in the compose file and the recorded image,
    so the harness prints both; the paths are read from the roles that own them.
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


#: What the CLI container can see. A path is addressable only under one of the
#: control plane service's bind mounts, and host and container names differ
#: (/var/lib/blitzecdn vs /opt/blitzecdn/.state). A mount is written as
#: `<host variable> ~ '<suffix>:<target>[:ro]'`, so the container name is the
#: second colon-separated field.
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

    The harness runs on the host and the CLI in a container, with different names
    for the same directory. The mounts are read from the service that declares
    them.
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

    Nginx serves the ACME webroot read-only as the image's uid, so a mode that
    admits only a host account answers 403. Through the shipped playbook, since
    the writer and server agreeing is the property.
    """
    commands = _commands(HTTP3_HARNESS)

    playbook = "src/blitzecdn/capabilities/tls/ansible/playbooks/acme-challenge.yml"
    assert (REPO_ROOT / playbook).is_file(), "the challenge playbook moved"
    assert playbook in commands, (
        "the harness no longer runs the shipped challenge playbook, so it "
        "proves nothing about the writer the control plane actually uses"
    )

    # Published and withdrawn: a token outliving its order could answer the next
    # validation. The helper's name is found rather than assumed.
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

    # Over port 80, where a CA looks, checking the body: wrong bytes fail
    # validation too.
    assert "site-one.test:80:${edge_ip}" in commands, (
        "the challenge is not fetched over the port ACME uses"
    )
    assert "ACME_VALIDATION" in commands.split("body=")[1][:200], (
        "the harness does not compare the served body with the validation value"
    )


def test_the_install_harness_issues_against_a_ca_that_really_validates():
    """The control plane's half of ACME, and the ways it could pass hollow.

    Issuance is certbot, the manual hooks, the playbook they drive and the chain
    the store validates. The key setting is `httpPort`: Pebble's default 5002 is
    its own challenge server, which would issue without asking the edge.
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

    # The configuration change must reach the running process; `up --detach` alone
    # can leave up-to-date services on the old configuration.
    assert "--force-recreate" in commands, (
        "the control plane is not replaced after being reconfigured, so "
        "issuance can run against the CA it was already pointed at"
    )
    assert "StartedAt" in commands, "nothing checks that the restart replaced anything"

    # Through the product's own issuance path, not by driving certbot directly.
    assert "/certificate/request" in commands, (
        "the harness does not ask the control plane to issue the certificate"
    )

    # certbot reaches Pebble through the committed wrapper, which must be present
    # and executable in the image.
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

    # The chain is verified against its root and for its name, or the last check is
    # only "something answered on 443".
    assert "-CAfile" in commands and "-verify_return_error" in commands, (
        "the harness does not verify the served chain against the ACME root"
    )
    assert "-verify_hostname" in commands, (
        "the harness does not check the served certificate covers the name"
    )


def test_the_install_harness_proves_a_renewal_changed_something():
    """Renewal's failure mode is succeeding without doing anything.

    `cert renew` exits zero with skipped sites. The certificate the edge presents
    is read from the wire before and after and must differ.
    """
    commands = _commands(INSTALL_HARNESS)

    # The renewal's own invocation: `--force` also appears in `--force-recreate`.
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

    # The DNS server the CA and preflight depend on is checked for answers:
    # `docker run -d` succeeds for a container that starts and dies.
    assert re.search(r"dig .*@127\.0\.0\.1", commands), (
        "nothing checks that the challenge DNS server actually resolves"
    )

    # Otherwise Pebble reuses an authorization about half the time, and whether
    # the renewal revalidates over HTTP-01 is decided per run by a coin toss.
    assert "PEBBLE_AUTHZREUSE=0" in commands, (
        "authorization reuse is left at its default, so the renewal's coverage "
        "varies from run to run"
    )
