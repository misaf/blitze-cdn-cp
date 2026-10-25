"""Container health.

Docker answers one question about an edge (is Nginx serving right now) and
Ansible answers the rest once per deploy. These hold that split.
"""

import re

import yaml
from support.contracts import (
    PROJECT_DIR,
    ROLE_DIR,
    STACK_ROLE_DIR,
    _ansible_jinja,
    _role,
    _role_defaults,
)
from support.control_plane import build_application
from support.paths import CORE_ANSIBLE
from support.role_contracts import (
    COMPOSE_TEMPLATE,
    _defaults_of,
    _edge_context,
    _render_compose,
)

from blitzecdn.assets.docker import (
    EDGE_DOCKERFILE,
)


def test_the_container_health_check_reads_the_configured_status_endpoint():
    """Docker's probe is the deployment's URL, not a URL baked into an image.

    The address, port and path are rendered by blitzecdn_nginx, so moving the
    endpoint moves the probe.
    """
    context = _edge_context()
    service = _render_compose()["services"]["edge"]
    healthcheck = service["healthcheck"]
    url = (
        f"http://{context['blitzecdn_edge_runtime']['status']['address']}"
        f":{context['blitzecdn_edge_runtime']['status']['port']}"
        f"{context['blitzecdn_edge_runtime']['status']['path']}"
    )

    # Exec form: no shell in the container to quote the URL wrong.
    assert healthcheck["test"][0] == "CMD"
    assert healthcheck["test"][1] == "curl"
    assert healthcheck["test"][-1] == url
    # --fail is what makes a 404 or a 502 unhealthy rather than "curl exited 0
    # having written a response body to /dev/null".
    assert "--fail" in healthcheck["test"]
    assert healthcheck["interval"] == "30s"
    assert healthcheck["timeout"] == "5s"
    assert healthcheck["start_period"] == "20s"
    assert healthcheck["retries"] == 3
    # The endpoint is loopback-bound and the container shares the host's
    # network namespace. Neither the probe nor anything else publishes it.
    assert context["blitzecdn_edge_runtime"]["status"]["address"] == "127.0.0.1"
    assert "network_mode: host" in COMPOSE_TEMPLATE


def test_the_container_health_check_asks_nothing_ansible_asks_better():
    """One question in Compose, the deep ones in health.yml.

    `nginx -t` in the probe cannot fail a deploy and re-reads every certificate
    each time. In health.yml the same failure fails the converge and reaches the
    rescue path.
    """
    health = (STACK_ROLE_DIR / "tasks/health.yml").read_text(encoding="utf-8")
    edge = _render_compose()["services"]["edge"]
    healthcheck = edge["healthcheck"]

    assert healthcheck["test"].count("CMD") == 1
    for deeper in ("nginx", "-t", "ss", "kill", "test"):
        assert deeper not in healthcheck["test"], deeper

    assert "['nginx', '-t']" in health
    assert "current/nginx.conf" in health
    # The status endpoint is verified a second time by Ansible, from the
    # controller's side of the deploy, and separately from Docker's verdict.
    assert "Read the local status endpoint" in health
    assert "status_code: [200]" in health
    assert "Require every public TCP listener" in health
    assert "Require the HTTP/3 listener on UDP/443" in health
    # A capability's own preconditions are checked by its own role (GeoIP by
    # `blitzecdn_geoip`), not here.
    assert "geoip" not in health.lower()

    # And Compose is still asked to wait for Docker's verdict before the
    # converge continues into those checks.
    tasks = (STACK_ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    assert "community.docker.docker_compose_v2" in tasks
    assert "wait: true" in tasks
    assert tasks.index("docker_compose_v2") < tasks.index("import_tasks: health.yml")


def test_the_status_endpoint_and_the_health_check_are_not_optional(settings):
    """Every edge serves stub_status, and every edge carries a probe of it.

    Probing a managed virtual host instead would write into that site's log and
    cache. Both are part of the runtime contract: rendered unconditionally, no
    conditional around `healthcheck`, and a missing Docker verdict fails
    health.yml.
    """
    control = build_application(settings=settings)
    status_conf = (ROLE_DIR / "templates/status.conf.j2").read_text(encoding="utf-8")
    fixture = yaml.safe_load(
        (PROJECT_DIR / "tests/fixtures/desired-state.yml").read_text(encoding="utf-8")
    )
    try:
        for sites in (fixture["blitzecdn_nginx_sites"], []):
            bundle = control.releases.renderer.render(
                {
                    **fixture,
                    "blitzecdn_nginx_sites": sites,
                    "blitzecdn_release_digest": "a" * 64,
                }
            )
            assert "nginx/conf.d/blitzecdn-status.conf" in bundle.files
    finally:
        control.close()
    assert "{% if" not in status_conf

    assert "healthcheck" in _render_compose()["services"]["edge"]
    # Read from the template: nothing guards the block, so it reaches every edge.
    # The lines above it are the probe's comment and the logging options.
    lines = COMPOSE_TEMPLATE.splitlines()
    above = lines[: lines.index("    healthcheck:")]
    preceding = [line for line in above if not line.strip().startswith("#")][-1]
    assert not preceding.strip().startswith("{%"), preceding

    health = (STACK_ROLE_DIR / "tasks/health.yml").read_text(encoding="utf-8")
    assert "| default('missing') == 'healthy'" in health
    # The endpoint is asked about unconditionally too.
    endpoint_task = health.split("Read the local status endpoint")[1]
    assert "blitzecdn_edge_stack_status_" + "enabled" not in endpoint_task


def test_the_health_timeout_stays_configurable():
    """Health is mandatory; how long a cold edge gets to reach it is not."""
    defaults = _defaults_of(STACK_ROLE_DIR)
    assert isinstance(defaults["blitzecdn_edge_stack_health_timeout"], int)

    spec = yaml.safe_load(
        (STACK_ROLE_DIR / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )["argument_specs"]["main"]["options"]
    assert spec["blitzecdn_edge_stack_health_timeout"]["type"] == "int"

    wait = '    wait_timeout: "{{ blitzecdn_edge_stack_health_timeout | int }}"'
    for name in ("tasks/main.yml", "tasks/rollback.yml"):
        tasks = (STACK_ROLE_DIR / name).read_text(encoding="utf-8")
        # Both the converge and the rollback wait, and both wait unconditionally.
        assert "wait: true" in tasks, name
        assert wait in tasks, name


def test_the_runtime_and_its_engine_are_not_optional():
    """Neither role offers to skip itself, because neither `false` is a state.

    An edge with open ports must run the container serving them, and every
    BlitzeCDN process is a container. A `when:` around either would turn "opted
    out" into a converge that succeeds having done nothing.
    """
    for role, spec_defaults in (
        ("blitzecdn_edge_stack", STACK_ROLE_DIR),
        ("blitzecdn_docker", _role("blitzecdn_docker")),
    ):
        defaults = _defaults_of(spec_defaults)
        assert f"{role}_enabled" not in defaults
        spec = yaml.safe_load(
            (spec_defaults / "meta/argument_specs.yml").read_text(encoding="utf-8")
        )["argument_specs"]["main"]["options"]
        assert f"{role}_enabled" not in spec

    # The converge block stays — it is what gives main.yml one rescue — but it
    # carries no condition of its own.
    tasks = (STACK_ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    document = yaml.safe_load(tasks)
    converge = document[0]
    assert "when" not in converge, converge.get("when")
    assert "rescue" in converge
    assert yaml.safe_load(
        (_role("blitzecdn_docker") / "tasks/main.yml").read_text(encoding="utf-8")
    )[0].keys() == {"name", "block"}


def test_every_configuration_activation_is_guarded_by_nginx_t():
    """A rendered tree reaches a running edge only through `nginx -t`.

    Nginx keeps serving what it loaded, so an invalid file is silent until the
    next reload. Every writer notifies the handler, the handler validates before
    it signals, and the stack role re-validates against a new image before the
    serving container is replaced.
    """
    handlers = (ROLE_DIR / "handlers/main.yml").read_text(encoding="utf-8")
    listeners = yaml.safe_load(handlers)
    names = [task["name"] for task in listeners]
    assert names.index("Validate Nginx configuration") < names.index(
        "Reload Nginx after validation"
    ), "the reload handler must run after the validation it depends on"
    validate = listeners[names.index("Validate Nginx configuration")]
    # Candidate writes suppress the legacy handler; activation is explicit and
    # uses the sealed tree which staging already tested with the pinned image.
    assert validate["when"] == (
        "not ansible_check_mode and not "
        "(blitzecdn_config_release_enabled | default(false) | bool)"
    )
    assert validate["ansible.builtin.include_tasks"] == "config-test.yml"

    config_test = yaml.safe_load(
        (ROLE_DIR / "tasks/config-test.yml").read_text(encoding="utf-8")
    )
    assert len(config_test) == 1
    command = config_test[0]["community.docker.docker_container"]["command"]
    assert "['nginx', '-t']" in command
    assert "blitzecdn_config_release_candidate ~ '/nginx.conf'" in command
    assert "when" not in config_test[0]
    assert "failed_when" in config_test[0]

    # Nothing on the edge writes the configuration tree except staging, and
    # every staged candidate is tested before it can be activated.
    nginx_tasks = (ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    assert "ansible.builtin.template" not in nginx_tasks
    release_role = CORE_ANSIBLE / "roles/blitzecdn_config_release/tasks"
    phases = [
        task["ansible.builtin.import_tasks"]
        for task in yaml.safe_load((release_role / "main.yml").read_text())
    ]
    assert (
        phases.index("stage.yml")
        < phases.index("seal.yml")
        < phases.index("activate.yml")
    )
    assert "tasks_from: config-test.yml" in (release_role / "seal.yml").read_text()

    # And an upgrade validates against the image it is moving to, before the
    # container running the old one is replaced.
    stack_tasks = (STACK_ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    assert "tasks_from: config-test.yml" in stack_tasks
    assert stack_tasks.index("config-test.yml") < stack_tasks.index("docker_compose_v2")


def test_the_status_endpoint_is_loopback_only():
    """Two independent controls, because stub_status has no authentication.

    The listen address binds loopback and the location's allow/deny list refuses
    anything else, so widening one alone does not expose it.
    """
    defaults = _role_defaults()
    assert defaults["blitzecdn_edge_runtime"]["status"]["address"] == "127.0.0.1"
    assert defaults["blitzecdn_nginx_status_allow"] == ["127.0.0.1", "::1"]

    status = (ROLE_DIR / "templates/status.conf.j2").read_text(encoding="utf-8")
    rendered = _ansible_jinja().from_string(status).render(**_edge_context())
    assert "listen 127.0.0.1:8090;" in rendered
    for allowed in ("allow 127.0.0.1;", "allow ::1;"):
        assert allowed in rendered, allowed
    assert "deny all;" in rendered
    # Anything else on the port is refused without confirming the port is open.
    assert "return 444;" in rendered

    # The container reaches it over the host network namespace, so the probe
    # needs no published port and the endpoint stays unreachable from outside.
    healthcheck = _render_compose()["services"]["edge"]["healthcheck"]
    assert "http://127.0.0.1:8090/stub_status" in healthcheck["test"]


def test_the_edge_stops_gracefully():
    """SIGQUIT and a grace period, not SIGTERM and a kill.

    SIGTERM is Nginx's immediate shutdown. Both the signal and enough time before
    SIGKILL must hold.
    """
    edge = _render_compose()["services"]["edge"]
    assert edge["stop_signal"] == "SIGQUIT"
    grace = str(edge["stop_grace_period"])
    assert grace.endswith("s") and int(grace[:-1]) >= 10, grace

    # And the image agrees, for a `docker stop` outside Compose.
    dockerfile = EDGE_DOCKERFILE.read_text(encoding="utf-8")
    assert "STOPSIGNAL SIGQUIT" in dockerfile


def test_no_edge_image_reference_floats():
    """A floating tag makes "which build is this fleet running" unanswerable.

    Every shipped reference is an exact tag or a digest, and the converge pins
    what it pulled to a digest before the compose file names it.
    """
    group_vars = yaml.safe_load(
        (CORE_ANSIBLE / "inventory/group_vars/blitzecdn_edges/defaults.yml").read_text(
            encoding="utf-8"
        )
    )
    assert group_vars["blitzecdn_edge_image_tag"] not in ("latest", "", None)

    # Every image reference in the workspace, found rather than listed.
    references = {
        f"{path}:{name}": str(value).strip()
        for path in sorted(PROJECT_DIR.glob("**/roles/*/defaults/main.yml"))
        if ".venv" not in path.parts and "collections" not in path.parts
        for name, value in (
            yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ).items()
        # A literal reference only; a default derived from another variable is covered
        # where its source is written.
        if isinstance(value, str)
        and "image" in name
        and "{{" not in value
        # A registry reference, not a host path: the rollback record lives at
        # /var/lib/blitzecdn/edge/image and is not an image at all.
        and re.match(r"[^/\s]+\.[^/\s]+/", value)
    }
    assert any("blitzecdn_edge_runtime_image_default" in key for key in references)
    assert any("geoipupdate" in value for value in references.values())
    for name, reference in references.items():
        assert not reference.endswith(":latest"), name
        assert re.search(r"(@sha256:[0-9a-f]{64}|:\d+\.\d+)", reference), reference
    # One fallback: blitzecdn_edge_runtime.image, which blitzecdn_nginx validates
    # against and blitzecdn_edge_stack serves from.
    assert "image" not in _defaults_of(STACK_ROLE_DIR).get("blitzecdn_edge_stack", {})
    for role_dir in (ROLE_DIR, STACK_ROLE_DIR):
        source = (role_dir / "defaults/main.yml").read_text(encoding="utf-8")
        assert "ghcr.io/misaf/blitzecdn-edge" not in source, role_dir.name

    # The pull resolves to a digest, and Compose is forbidden from pulling
    # again — a floating tag resolved twice in one run can resolve twice.
    image = (STACK_ROLE_DIR / "tasks/image.yml").read_text(encoding="utf-8")
    assert "RepoDigests" in image
    for name in ("tasks/main.yml", "tasks/rollback.yml"):
        assert "pull: never" in (STACK_ROLE_DIR / name).read_text(encoding="utf-8")


def test_a_preview_resolves_the_image_a_converge_would_actually_run():
    """Check mode must not fall through to the tag when a digest is recorded.

    A preview rendering the tag would disagree with the converged file on that
    line and report every edge as drifted.
    """
    tasks = yaml.safe_load(
        (STACK_ROLE_DIR / "tasks/image.yml").read_text(encoding="utf-8")
    )
    reuse = [
        task
        for task in tasks
        if task["name"]
        in (
            "Check whether the recorded image is still on this host",
            "Reuse the recorded image",
        )
    ]
    assert len(reuse) == 2
    for task in reuse:
        conditions = task.get("when", [])
        conditions = [conditions] if isinstance(conditions, str) else conditions
        assert not any("ansible_check_mode" in str(item) for item in conditions), (
            f"{task['name']} is skipped in check mode, so a preview cannot see "
            "the digest the last converge pinned and reports drift against it"
        )

    # The tag is the last resort, reached only when nothing resolved an image.
    fetch = next(task for task in tasks if "block" in task)
    assert "blitzecdn_edge_stack_resolved_image is not defined" in fetch["when"]
    preview = next(
        task for task in fetch["block"] if task["name"] == "Preview the requested image"
    )
    assert preview["when"] == "ansible_check_mode"


def test_a_failed_converge_always_reaches_the_rollback():
    """Rescue is not conditional, and the rollback proves what it restored.

    Everything that changes the runtime is in one block, so any failure is caught,
    and the rollback re-checks health rather than assuming it.
    """
    converge = yaml.safe_load(
        (STACK_ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    )[0]
    rescue = converge["rescue"]
    assert [task["ansible.builtin.import_tasks"] for task in rescue] == ["rollback.yml"]
    assert all("when" not in task for task in rescue)

    body = converge["block"]
    names = [task["name"] for task in body]
    assert "Verify the edge is serving" in names
    assert names.index("Start or update the edge stack") < names.index(
        "Verify the edge is serving"
    )
    # The image record is written only after the health checks, so a failed
    # image can never become the thing a later rollback returns to.
    assert names.index("Verify the edge is serving") < names.index(
        "Record the deployed edge runtime image"
    )

    rollback = yaml.safe_load(
        (STACK_ROLE_DIR / "tasks/rollback.yml").read_text(encoding="utf-8")
    )
    restore = next(
        task for task in rollback if task["name"].startswith("Return this edge")
    )
    inner = [task["name"] for task in restore["block"]]
    assert "Verify the previous runtime is serving" in inner
    # And it still fails: an edge that had to be rolled back has not deployed.
    assert inner[-1].startswith("Report the withdrawn")


def test_no_documentation_claims_docker_restarts_an_unhealthy_container():
    """`restart: unless-stopped` reacts to the main process exiting, and only that.

    Docker has no restart-on-unhealthy; health.yml's assertion is what turns a
    wedged edge into a failed deploy.
    """
    assert "restart: unless-stopped" in COMPOSE_TEMPLATE
    health = (STACK_ROLE_DIR / "tasks/health.yml").read_text(encoding="utf-8")
    defaults = (STACK_ROLE_DIR / "defaults/main.yml").read_text(encoding="utf-8")

    for document in (COMPOSE_TEMPLATE, health, defaults):
        for claim in (
            "will be restarted underneath us",
            "keep restarting underneath us",
            "restarted underneath",
            "Docker will restart",
        ):
            assert claim not in document, claim

    # Said outright rather than merely not said wrongly, so the next reader of
    # either file learns what the health state is actually for.
    assert "does not restart a still-running container" in health
    assert "does not restart a running container" in defaults
    assert "does not restart it" in COMPOSE_TEMPLATE


def test_the_edge_image_carries_curl_and_no_health_check_script_of_its_own():
    """The probe lives in Compose; the tool it runs stays in the image.

    An image HEALTHCHECK would hard-code the endpoint the deployment owns. curl is
    the dependency the Compose probe execs.
    """
    dockerfile = EDGE_DOCKERFILE.read_text(encoding="utf-8")

    assert not (PROJECT_DIR / ("healthcheck" + ".sh")).exists()
    assert not (EDGE_DOCKERFILE.parent / ("healthcheck" + ".sh")).exists()
    # The instruction, not the word: the comment explaining its absence stays.
    assert not any(line.startswith("HEALTHCHECK") for line in dockerfile.splitlines())
    assert "healthcheck" + ".sh" not in dockerfile
    assert "blitzecdn-" + "healthcheck" not in dockerfile
    # In the runtime stage, not just the throwaway builder that also uses it.
    assert "curl" in dockerfile.split("FROM ${NGINX_IMAGE}")[-1]

    # test_removed_host_compatibility_contracts_do_not_reappear walks the tree for
    # the old script and its variable.
    assert "BLITZECDN_" + "HEALTH_URL" not in COMPOSE_TEMPLATE


def test_the_edge_lifecycle_order_holds():
    """Two orderings, each of which has cost an outage when it was wrong.

    The engine, persistent state and image are prepared before the firewall opens
    public ports. The container starts only after blitzecdn_nginx has rendered and
    validated the configuration.
    """
    play = (CORE_ANSIBLE / "playbooks/edge.yml").read_text(encoding="utf-8")
    order = [
        "name: blitzecdn_docker",
        "tasks_from: prepare.yml",
        "tasks_from: verify-runtime.yml",
        "role: blitzecdn_firewall",
        "role: blitzecdn_nginx",
        "role: blitzecdn_config_release",
    ]
    positions = [play.index(marker) for marker in order]
    assert positions == sorted(positions), (
        "the edge lifecycle is out of order; expected " + " -> ".join(order)
    )
    # The stack role is the last to touch the runtime, and the host capability slot
    # stays after it: nothing that could close the management path may run before
    # the runtime proves it serves.
    assert play.index("role: blitzecdn_config_release") < play.index(
        "blitzecdn_host_capability_roles"
    )


def test_the_fleet_rollout_starts_with_one_edge():
    """A canary batch, and `any_errors_fatal` to make it mean something.

    Wider batches without the flag would roll a broken image across the fleet;
    the flag without a first batch of one would take a quarter down first.
    `max_fail_percentage` is absent so the policy is stated once.
    """
    play = yaml.safe_load(
        (CORE_ANSIBLE / "playbooks/edge.yml").read_text(encoding="utf-8")
    )[0]

    assert play["any_errors_fatal"] is True
    assert "max_fail_percentage" not in play
    assert play["serial"][0] == 1, (
        "the edge rollout no longer starts with a single canary edge"
    )
    assert play["serial"][-1] == "100%"
    # Monotonic, so a batch is never smaller than the one that preceded it.
    widths = [1] + [int(str(step).rstrip("%")) for step in play["serial"][1:]]
    assert widths == sorted(widths), play["serial"]


def test_the_image_is_settable_as_ordinary_fleet_policy():
    """The README tells operators to roll out an image with `config set`.

    Settings with credential-shaped words such as "key" are refused, so the name
    must avoid them.
    """
    from blitzecdn.core.domain.validation import validate_setting_name

    for name in (
        "blitzecdn_edge_image",
        "blitzecdn_edge_image_tag",
        "blitzecdn_edge_image_digest",
        "blitzecdn_edge_stack_image_pull",
    ):
        assert validate_setting_name(name) == name
