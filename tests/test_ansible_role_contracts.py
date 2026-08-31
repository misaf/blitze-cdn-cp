# ruff: noqa: F403,F405
from contract_support import *


def test_http3_uses_the_firewall_registry_for_udp_443():
    role = _role("blitzecdn_firewall")
    tasks = (role / "tasks/main.yml").read_text(encoding="utf-8")
    play = (PROJECT_DIR / "ansible/playbooks/edge.yml").read_text(encoding="utf-8")

    assert "['udp|443|any'] if blitzecdn_edge_runtime.listeners.http3 else []" in tasks
    assert "proto: udp" in tasks
    assert "when: blitzecdn_edge_runtime.listeners.http3 | bool" in tasks
    assert "^(tcp|udp)\\|" in tasks
    assert "difference(blitzecdn_firewall_desired_rules)" in tasks
    assert play.index("tasks_from: verify-runtime.yml") < play.index(
        "role: blitzecdn_firewall"
    )
    # And it opens exactly the ports the Nginx role binds, because both read
    # one list. There is no second copy left to keep in step.
    assert "blitzecdn_firewall_http_ports" not in tasks
    assert "blitzecdn_firewall_http_ports" not in (
        role / "defaults/main.yml"
    ).read_text(encoding="utf-8")


def test_the_udp_443_listener_is_verified_where_the_firewall_opened_it():
    """The firewall and the QUIC listener must not be able to disagree.

    The play refuses to converge when the two *desired* states differ. That is
    only half of it: a QUIC bind that fails inside the container leaves UDP/443
    open in the firewall, Alt-Svc advertised to every visitor, and nothing
    listening — an edge that tells browsers to use a protocol it does not
    serve. The health check is the other half.
    """
    health = (STACK_ROLE_DIR / "tasks/health.yml").read_text(encoding="utf-8")
    assert "ss" in health and "-lnu" in health
    assert "blitzecdn_edge_runtime.listeners.http3" in health
    assert "search(':443" in health


def test_docker_daemon_configuration_uses_only_supported_directives():
    """A JSON pseudo-comment is a real key that makes dockerd refuse to start."""
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
    environment.filters["bool"] = bool
    rendered = environment.from_string(
        (DOCKER_ROLE_DIR / "templates/daemon.json.j2").read_text(encoding="utf-8")
    ).render(
        blitzecdn_docker_log_driver="json-file",
        blitzecdn_docker_log_max_size="32m",
        blitzecdn_docker_log_max_file=3,
        blitzecdn_docker_live_restore=True,
    )
    configuration = yaml.safe_load(rendered)

    assert set(configuration) == {"log-driver", "log-opts", "live-restore"}

    tasks = yaml.safe_load(
        (DOCKER_ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    )
    configure = next(
        task
        for task in tasks[0]["block"]
        if task["name"] == "Configure the Docker daemon"
    )
    assert configure["ansible.builtin.template"]["validate"] == (
        "/usr/bin/dockerd --validate --config-file %s"
    )


# ----------------------------------------------------------------------
# The containerised runtime
#
# Nginx runs in a container and its configuration does not. Everything below
# guards a seam between the two: an image that is validated but not the one
# that serves, a mount the configuration test cannot see, a persistent path
# nothing creates, a runtime removal that takes a customer's keys with it.
# ----------------------------------------------------------------------


def test_geoip_task_result_does_not_overwrite_the_role_input():
    """The role is imported twice, so registered results must preserve inputs."""
    defaults = _defaults_of(STACK_ROLE_DIR)
    tasks = (STACK_ROLE_DIR / "tasks/geoip-database.yml").read_text(encoding="utf-8")

    assert isinstance(defaults["blitzecdn_edge_stack_geoip_units"], list)
    assert "register: blitzecdn_edge_stack_geoip_units\n" not in tasks
    assert "register: blitzecdn_edge_stack_geoip_units_rendered\n" in tasks
    assert "blitzecdn_edge_stack_geoip_units_rendered is changed" in tasks


COMPOSE_TEMPLATE = (STACK_ROLE_DIR / "templates/compose.yml.j2").read_text(
    encoding="utf-8"
)


def _edge_context(**overrides: Any) -> dict[str, Any]:
    """The variables the edge stack renders from, with references resolved.

    The paths, the image and the status endpoint are blitzecdn_edge_runtime's;
    what remains in blitzecdn_edge_stack's defaults derives from them. Resolving
    both here is what lets these tests read the *paths*, which is what the
    mounts actually are.
    """
    inputs, plain = _split_runtime(overrides)
    context: dict[str, Any] = (
        _role_defaults(**inputs) | _defaults_of(STACK_ROLE_DIR) | plain
    )
    environment = _ansible_jinja()
    for _ in range(len(context)):
        unresolved = {
            name: value
            for name, value in context.items()
            if isinstance(value, str) and "{{" in value
        }
        if not unresolved:
            break
        for name, value in unresolved.items():
            context[name] = environment.from_string(value).render(**context).strip()
        if unresolved.keys() == {
            name
            for name, value in context.items()
            if isinstance(value, str) and "{{" in value
        }:
            break
    return context


def _render_compose(**overrides: Any) -> dict[str, Any]:
    """The edge Compose project as Docker would read it, not as text."""
    context = _edge_context(**overrides)
    environment = _ansible_jinja(
        loader=jinja2.FileSystemLoader(STACK_ROLE_DIR / "templates"),
        keep_trailing_newline=True,
    )
    rendered = environment.get_template("compose.yml.j2").render(
        blitzecdn_edge_stack_resolved_image="example/edge@sha256:" + "ab" * 32,
        **context,
    )
    return yaml.safe_load(rendered)


def _compose_mounts(**overrides: Any) -> dict[str, str]:
    """host path -> mode, for every bind mount the rendered edge service has."""
    project = _render_compose(**overrides)
    mounts: dict[str, str] = {}
    for entry in project["services"]["edge"]["volumes"]:
        source, _, mode = entry.split(":")
        mounts[source] = mode
    return mounts


def test_the_configuration_test_sees_what_the_running_edge_sees():
    """A probe with fewer mounts passes against a tree the real edge refuses.

    `nginx -t` opens every certificate, every njs module and the GeoIP
    database. A configuration test that cannot reach one of them reports a
    valid configuration, the deploy commits it, and the container that is then
    started — or reloaded — fails on the file the test never looked at.
    """
    probe = {
        entry.split(":")[0]: entry.split(":")[2]
        for entry in _edge_context(blitzecdn_edge_geoip_enabled=True)[
            "blitzecdn_nginx_config_test_volumes"
        ]
    }
    # Rendered with GeoIP on, because the probe mounts the database
    # unconditionally and this has to compare the full set. The other direction
    # is the safe one: a probe that always sees the database can never miss one
    # the edge has.
    compose = _compose_mounts(blitzecdn_edge_geoip_enabled=True)
    assert compose == probe, (
        "the configuration test container and the running edge disagree about "
        "their mounts; a test that cannot see a file the edge reads passes "
        "against a configuration the edge would refuse"
    )


def test_only_cache_and_logs_are_writable_to_the_edge():
    """Configuration and TLS material are read-only mounts, deliberately.

    An edge has no business rewriting what the control plane rendered, and a
    container that cannot write its own configuration cannot be talked into
    persisting a change the next converge would silently revert.
    """
    runtime = _role_defaults()["blitzecdn_edge_runtime"]
    writable = {path for path, mode in _compose_mounts().items() if mode == "rw"}
    assert writable == {runtime["paths"]["cache"], runtime["paths"]["logs"]}


def test_every_mounted_path_is_created_before_the_container_starts():
    """Docker creates a missing bind source as an empty root-owned directory.

    Which means a path nothing creates does not fail: it silently becomes an
    empty directory, and the edge starts with no configuration and answers 444
    for every customer.
    """
    created = "\n".join(
        source.read_text(encoding="utf-8")
        for source in (
            STACK_ROLE_DIR / "tasks/prepare.yml",
            STACK_ROLE_DIR / "tasks/geoip-credentials.yml",
            ROLE_DIR / "tasks/main.yml",
        )
    )
    context = _edge_context(blitzecdn_edge_geoip_enabled=True)
    # Paths are almost never written out in a task: they are composed from the
    # contract, as `{{ blitzecdn_edge_runtime.paths.nginx }}/conf.d`. So the
    # task text is substituted before it is searched, which is what lets this
    # compare directories rather than variable names — and keeps it honest when
    # a path is half literal and half contract.
    substitutions: dict[str, str] = {}

    def collect(prefix: str, value: Any) -> None:
        if isinstance(value, str) and value.startswith("/"):
            substitutions[prefix] = value
        elif isinstance(value, dict):
            for key, item in value.items():
                collect(f"{prefix}.{key}" if prefix else str(key), item)

    for name, value in context.items():
        collect(name, value)
    for name, value in sorted(substitutions.items(), key=lambda item: -len(item[0])):
        created = created.replace("{{ " + name + " }}", value)

    for path in _compose_mounts(blitzecdn_edge_geoip_enabled=True):
        assert path in created, (
            f"{path} is mounted into the edge container but nothing creates it; "
            "Docker would make it an empty directory and the edge would serve "
            "nothing from it"
        )


def test_the_edge_container_takes_the_host_network():
    """Bridged, $remote_addr is a gateway address and the CDN lies to itself.

    Every per-site source rule, the GeoIP2 country lookup and BZ-Connecting-IP
    all read $remote_addr. Behind Docker's userland proxy that is the bridge
    gateway, so an edge would happily apply a country rule to itself — and the
    published-port list would become a second copy of the supported ports to
    keep in step.
    """
    assert "network_mode: host" in COMPOSE_TEMPLATE
    assert "ports:" not in COMPOSE_TEMPLATE
    assert "expose:" not in COMPOSE_TEMPLATE


def test_a_configuration_change_reloads_and_does_not_replace_the_container():
    """Replacing the container for a site change drops every live connection.

    It also empties the shared-memory cache zone. Nginx applies a new
    configuration without dropping a request, so the handler signals the
    running container and the stack role leaves it alone.
    """
    handlers = (ROLE_DIR / "handlers/main.yml").read_text(encoding="utf-8")
    assert "nginx, -s, reload" in handlers
    assert "docker_container_exec" in handlers
    assert _defaults_of(STACK_ROLE_DIR)["blitzecdn_edge_stack_recreate"] is False


def test_a_rollback_returns_to_a_recorded_digest():
    """ "Put the old one back" is only possible if the old one has a name.

    By the time a rollback is needed the tag that was running an hour ago may
    point somewhere else, so restoring a tag would install a third unknown
    version. The record holds what was asked for and what it resolved to.
    """
    image = (STACK_ROLE_DIR / "tasks/image.yml").read_text(encoding="utf-8")
    rollback = (STACK_ROLE_DIR / "tasks/rollback.yml").read_text(encoding="utf-8")
    assert "RepoDigests" in image
    assert "blitzecdn_edge_stack_deployed_image_file" in rollback
    assert "blitzecdn_edge_stack_previous.resolved" in rollback
    # A first converge has nothing to return to, and must say so rather than
    # reporting an edge "rolled back and serving" that has never served.
    assert "has never served" in rollback


def test_teardown_separates_stopping_the_edge_from_erasing_it():
    """Two operations that must not be confused for one.

    The containers are disposable and always go. TLS material, the
    configuration tree, ACME state and the cache go only on request, so taking
    the runtime off a host that is about to be rebuilt cannot destroy what it
    was serving.
    """
    teardown = _role("blitzecdn_teardown")
    tasks = yaml.safe_load((teardown / "tasks/main.yml").read_text(encoding="utf-8"))
    assert _defaults_of(teardown)["blitzecdn_teardown_remove_data"] is True

    def gate(name: str) -> object:
        return next(task for task in tasks if task["name"] == name).get("when")

    assert gate("Stop and remove the edge stack") != "blitzecdn_teardown_remove_data"
    for destructive in (
        "Remove the ACME webroot",
        "Remove cached responses",
        "Remove controller-written edge state and TLS material",
        "Remove the managed-site registry",
    ):
        assert gate(destructive) == "blitzecdn_teardown_remove_data", destructive


def _walk_ansible_tasks(value: Any):
    """Yield tasks, including tasks nested in block/rescue/always sections."""
    if not isinstance(value, list):
        return
    for task in value:
        if not isinstance(task, dict):
            continue
        yield task
        for section in ("block", "rescue", "always"):
            yield from _walk_ansible_tasks(task.get(section))


def _role_tasks():
    for role in sorted(ROLES_DIR.iterdir()):
        if not role.is_dir():
            continue
        for directory in (role / "tasks", role / "handlers"):
            if not directory.is_dir():
                continue
            for source in sorted(directory.glob("*.yml")):
                yield (
                    source,
                    _walk_ansible_tasks(
                        yaml.safe_load(source.read_text(encoding="utf-8"))
                    ),
                )


def test_no_host_role_installs_traffic_serving_packages():
    """Nginx and GeoIP updater packages belong only in runtime images."""
    forbidden = {
        "nginx",
        "nginx-common",
        "nginx-core",
        "libnginx-mod-http-geoip2",
        "libnginx-mod-http-brotli-filter",
        "libnginx-mod-http-js",
        "geoipupdate",
    }
    package_modules = ("ansible.builtin.apt", "ansible.builtin.package")
    for source, tasks in _role_tasks():
        for task in tasks:
            for module in package_modules:
                if module not in task:
                    continue
                arguments = yaml.safe_dump(task[module])
                for package in forbidden:
                    pattern = (
                        rf"(?<![A-Za-z0-9_-]){re.escape(package)}(?![A-Za-z0-9_-])"
                    )
                    assert re.search(pattern, arguments) is None, (
                        f"{source} installs forbidden host package {package}"
                    )


def test_no_host_task_controls_or_executes_nginx():
    """Host automation may signal Nginx only through docker_container_exec."""
    host_modules = (
        "ansible.builtin.systemd",
        "ansible.builtin.systemd_service",
        "ansible.builtin.service",
        "ansible.builtin.command",
        "ansible.builtin.shell",
    )
    nginx = re.compile(r"(?<![A-Za-z0-9_-])nginx(?![A-Za-z0-9_-])", re.I)
    for source, tasks in _role_tasks():
        for task in tasks:
            for module in host_modules:
                if module in task:
                    assert nginx.search(yaml.safe_dump(task[module])) is None, (
                        f"{source} controls or executes Nginx on the host"
                    )


def test_fresh_host_guard_is_validation_only_and_runs_first(tmp_path):
    guard_path = STACK_ROLE_DIR / "tasks/validate-host.yml"
    guard = yaml.safe_load(guard_path.read_text(encoding="utf-8"))
    prepare = (STACK_ROLE_DIR / "tasks/prepare.yml").read_text(encoding="utf-8")

    assert guard[0]["ansible.builtin.stat"]["path"] == "/usr/sbin/nginx"
    assert guard[1]["ansible.builtin.assert"]["that"] == [
        "not blitzecdn_edge_stack_host_nginx.stat.exists"
    ]
    message = guard[1]["ansible.builtin.assert"]["fail_msg"]
    assert "requires a fresh Ubuntu 26.04 LTS edge" in message
    assert "does not migrate or purge" in message
    assert "Rebuild the host" in message
    assert set(guard[0]) == {"name", "ansible.builtin.stat", "register"}
    assert set(guard[1]) == {"name", "ansible.builtin.assert"}
    assert prepare.index("validate-host.yml") < prepare.index(
        "Create the persistent edge directories"
    )

    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")
    playbook = tmp_path / "fresh-host-guard.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "blitzecdn_edge_stack_host_nginx": {"stat": {"exists": True}}
                    },
                    "tasks": [guard[1]],
                }
            ]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [ansible, "-i", "localhost,", "-c", "local", str(playbook)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode != 0
    assert "does not migrate or purge" in result.stdout


def test_removed_host_compatibility_contracts_do_not_reappear():
    forbidden = {
        "blitzecdn_edge_stack_migrate_" + "from_native",
        "blitzecdn_edge_stack_native_" + "packages",
        "/etc/" + "GeoIP.conf",
        # The standalone edge health check script and the variable that
        # configured it. Docker's probe is now the Compose healthcheck, which
        # reads the status endpoint straight from the role's variables.
        "blitzecdn-" + "healthcheck",
        "BLITZECDN_" + "HEALTH_URL",
        # The flags that once made the status endpoint and the container's
        # health optional. Both are part of the edge runtime contract now, so
        # there is no supported state for these to name.
        "blitzecdn_nginx_status_" + "enabled",
        "blitzecdn_edge_stack_status_" + "enabled",
        "blitzecdn_edge_stack_require_" + "container_health",
        # The switches that offered to skip the runtime itself. An edge with
        # its ports open and its configuration rendered has to run the
        # container serving them, and every BlitzeCDN process on both an edge
        # and the control plane is a container, so neither `false` named a
        # state this collection supports.
        "blitzecdn_edge_stack_" + "enabled",
        "blitzecdn_docker_" + "enabled",
    }
    ignored = {".git", ".venv", ".state", ".mypy_cache", ".pytest_cache"}

    def sources():
        # Pruned during the walk, not filtered after one: `.venv` alone holds
        # tens of thousands of files, and descending into it to discard every
        # entry was most of this test's runtime.
        for parent, directories, files in os.walk(PROJECT_DIR):
            directories[:] = [name for name in directories if name not in ignored]
            for name in files:
                path = Path(parent, name)
                if path.suffix in {".md", ".py", ".sh", ".yml", ".yaml", ".j2"}:
                    yield path

    for source in sources():
        document = source.read_text(encoding="utf-8")
        for obsolete in forbidden:
            assert obsolete not in document, f"{source} contains {obsolete}"


def test_the_edge_image_extends_the_pinned_official_image_with_abi_matched_modules():
    """Third-party modules must be built against the exact runtime image."""
    dockerfile = (PROJECT_DIR / "docker/edge/Dockerfile").read_text(encoding="utf-8")
    assert "ARG NGINX_IMAGE=nginx:1.31.4-alpine" in dockerfile
    assert dockerfile.count("FROM ${NGINX_IMAGE}") == 2
    assert 'ARG ENABLED_MODULES="geoip2 brotli"' in dockerfile
    assert '"${NGINX_VERSION}-${PKG_RELEASE}"' in dockerfile
    assert "nginx-module-geoip2-${NGINX_VERSION}*.apk" in dockerfile
    assert "nginx-module-brotli-${NGINX_VERSION}*.apk" in dockerfile
    assert "--with-http_v3_module" in dockerfile
    assert "module-probe.conf" in dockerfile
    assert "COPY modules.conf /etc/nginx/modules.conf" in dockerfile
    assert "include /etc/nginx/modules.conf;" in dockerfile
    assert "include /etc/nginx/sites-enabled/" in dockerfile

    probe = (PROJECT_DIR / "docker/edge/module-probe.conf").read_text(encoding="utf-8")
    modules = (PROJECT_DIR / "docker/edge/modules.conf").read_text(encoding="utf-8")
    assert "include /etc/nginx/modules.conf;" in probe
    for module in (
        "ngx_http_geoip2_module.so",
        "ngx_http_brotli_filter_module.so",
        "ngx_http_js_module.so",
    ):
        assert module in modules, module
    # A module that loads but registers no directive is indistinguishable from
    # a working one until an edge configuration uses it.
    assert "brotli off;" in probe
    assert "js_path" in probe

    capabilities = (
        STACK_ROLE_DIR.parent / "blitzecdn_nginx/tasks/capabilities.yml"
    ).read_text(encoding="utf-8")
    assert "read_only: true" in capabilities
    assert "/var/log/nginx:rw,noexec,nosuid,size=8m" in capabilities


def test_nginx_logs_to_persistent_files_and_docker_streams():
    """Docker logs supplement the retained files consumed by edge tooling."""
    dockerfile = (PROJECT_DIR / "docker/edge/Dockerfile").read_text(encoding="utf-8")
    site = (ROLE_DIR / "templates/site.conf.j2").read_text(encoding="utf-8")

    assert "error_log  /dev/stderr notice;" in dockerfile
    assert "access_log  /dev/stdout  main;" in dockerfile
    assert (
        site.count("access_log /dev/stdout {{ blitzecdn_nginx_log_format_name }};") == 2
    )
    assert "access_log {{ blitzecdn_nginx_access_log_path }}" in site
    assert 'max-size: "32m"' in COMPOSE_TEMPLATE
    assert 'max-file: "3"' in COMPOSE_TEMPLATE


# ----------------------------------------------------------------------
# Container health
#
# Docker answers one question about an edge — is this Nginx serving requests
# right now — and Ansible answers the rest once per deploy. The tests below
# hold that split in place: a Compose probe that grows a second question
# duplicates work the converge already does, and an Ansible check folded into
# the probe loses the failure that fails a deploy.
# ----------------------------------------------------------------------


def test_the_container_health_check_reads_the_configured_status_endpoint():
    """Docker's probe is the deployment's URL, not a URL baked into an image.

    The address, port and path are rendered by blitzecdn_nginx. A probe that
    restated any of them would keep reporting healthy — or unhealthy — after an
    operator moved the endpoint, which is exactly when the answer matters.
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

    `nginx -t` inside the probe cannot fail a deploy — Docker would simply mark
    a running container unhealthy every thirty seconds — and it re-reads every
    certificate and the GeoIP database each time. In health.yml the same
    failure fails the converge and reaches the rescue path.
    """
    health = (STACK_ROLE_DIR / "tasks/health.yml").read_text(encoding="utf-8")
    edge = _render_compose(blitzecdn_edge_geoip_enabled=True)["services"]["edge"]
    healthcheck = edge["healthcheck"]

    assert healthcheck["test"].count("CMD") == 1
    for deeper in ("nginx", "-t", "ss", "kill", "test"):
        assert deeper not in healthcheck["test"], deeper

    assert "argv: [nginx, -t]" in health
    # The status endpoint is verified a second time by Ansible, from the
    # controller's side of the deploy, and separately from Docker's verdict.
    assert "Read the local status endpoint" in health
    assert "status_code: [200]" in health
    assert "Require every public TCP listener" in health
    assert "Require the HTTP/3 listener on UDP/443" in health
    assert "Require the GeoIP database the running configuration reads" in health

    # And Compose is still asked to wait for Docker's verdict before the
    # converge continues into those checks.
    tasks = (STACK_ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    assert "community.docker.docker_compose_v2" in tasks
    assert "wait: true" in tasks
    assert tasks.index("docker_compose_v2") < tasks.index("import_tasks: health.yml")


def test_the_status_endpoint_and_the_health_check_are_not_optional():
    """Every edge serves stub_status, and every edge carries a probe of it.

    The alternative to the loopback endpoint would be probing a managed virtual
    host, which writes a request into that site's access log and its cache
    every thirty seconds. Rather than make either side switchable, both are part
    of the runtime contract: the nginx role renders the server unconditionally,
    the Compose template has no conditional around `healthcheck`, and a missing
    Docker verdict fails health.yml instead of being tolerated.
    """
    nginx_tasks = (ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    status_conf = (ROLE_DIR / "templates/status.conf.j2").read_text(encoding="utf-8")
    assert "Configure the status endpoint" in nginx_tasks
    assert "Withdraw the status endpoint" not in nginx_tasks
    # Rendered from a template with no enable/disable branch of its own.
    assert "{% if" not in status_conf

    assert "healthcheck" in _render_compose()["services"]["edge"]
    # Read from the template rather than one rendering: nothing guards the
    # block, so it reaches every edge and not just this test's context. The
    # lines above it are the probe's own comment and the logging options.
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

    An edge in the fleet has its public ports open and its configuration
    rendered; leaving the container that serves them unstarted advertises an
    edge that is not there. And every BlitzeCDN process on an edge and on the
    control plane is a container, so a host with no engine has nothing to run.
    Both guards were `when:` on a whole block, which is exactly the shape that
    turns "opted out" into a converge that reports success having done nothing.
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

    Nginx keeps serving what it loaded at start, so an invalid file is silent
    until the next reload — which may be a crash-restart hours later, with no
    deploy left to blame. There is one validation task file and no setting that
    skips it: every writer notifies the handler, the handler validates before
    it signals, and the stack role re-validates against a new image before the
    container serving traffic is replaced.
    """
    handlers = (ROLE_DIR / "handlers/main.yml").read_text(encoding="utf-8")
    listeners = yaml.safe_load(handlers)
    names = [task["name"] for task in listeners]
    assert names.index("Validate Nginx configuration") < names.index(
        "Reload Nginx after validation"
    ), "the reload handler must run after the validation it depends on"
    validate = listeners[names.index("Validate Nginx configuration")]
    # Check mode is the only condition: a validation that could be switched off
    # would leave the reload below it unguarded.
    assert validate["when"] == "not ansible_check_mode"
    assert validate["ansible.builtin.include_tasks"] == "config-test.yml"

    config_test = yaml.safe_load(
        (ROLE_DIR / "tasks/config-test.yml").read_text(encoding="utf-8")
    )
    assert len(config_test) == 1
    assert config_test[0]["community.docker.docker_container"]["command"] == [
        "nginx",
        "-t",
    ]
    assert "when" not in config_test[0]
    assert "failed_when" in config_test[0]

    # Every task that writes into the live tree hands the handler its cue.
    nginx_tasks = (ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    for writer in ("template:", "state: link", "state: absent"):
        assert writer in nginx_tasks
    assert nginx_tasks.count("notify: Validate and reload Nginx") >= 10

    # And an upgrade validates against the image it is moving to, before the
    # container running the old one is replaced.
    stack_tasks = (STACK_ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    assert "tasks_from: config-test.yml" in stack_tasks
    assert stack_tasks.index("config-test.yml") < stack_tasks.index("docker_compose_v2")


def test_the_status_endpoint_is_loopback_only():
    """Two independent controls, because stub_status has no authentication.

    It describes the edge's load to anyone who can reach it. The listen address
    binds loopback and the allow/deny list inside the location refuses anything
    else, so widening one alone still does not expose it — and there is no
    switch that publishes it.
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

    SIGTERM is Nginx's immediate shutdown, which turns every container
    replacement into a burst of visitor errors. Both halves have to hold: the
    signal, and enough time to act on it before Docker escalates to SIGKILL.
    """
    edge = _render_compose()["services"]["edge"]
    assert edge["stop_signal"] == "SIGQUIT"
    grace = str(edge["stop_grace_period"])
    assert grace.endswith("s") and int(grace[:-1]) >= 10, grace

    # And the image agrees, for a `docker stop` outside Compose.
    dockerfile = (PROJECT_DIR / "docker/edge/Dockerfile").read_text(encoding="utf-8")
    assert "STOPSIGNAL SIGQUIT" in dockerfile


def test_no_edge_image_reference_floats():
    """A floating tag makes "which build is this fleet running" unanswerable.

    It also makes rollback a guess: the bytes that were serving an hour ago
    have no name. Every reference this repository ships is an exact tag or a
    digest, and the converge pins whatever it pulled to a digest before the
    compose file names it.
    """
    group_vars = yaml.safe_load(
        (
            PROJECT_DIR / "ansible/inventory/group_vars/blitzecdn_edges/defaults.yml"
        ).read_text(encoding="utf-8")
    )
    assert group_vars["blitzecdn_edge_image_tag"] not in ("latest", "", None)

    runtime = _runtime_source()
    defaults = _defaults_of(STACK_ROLE_DIR)
    for source, name in (
        (runtime, "blitzecdn_edge_runtime_image_default"),
        (defaults, "blitzecdn_edge_stack_geoipupdate_image"),
    ):
        reference = str(source[name]).strip()
        assert not reference.endswith(":latest"), name
        assert re.search(r"(@sha256:[0-9a-f]{64}|:\d+\.\d+)", reference), reference
    # The two roles used to carry identical fallback literals, and a test
    # asserted they agreed. There is one now: blitzecdn_edge_runtime.image,
    # which blitzecdn_nginx validates against and blitzecdn_edge_stack serves
    # from, so agreeing is no longer something either role can fail at.
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


def test_a_failed_converge_always_reaches_the_rollback():
    """Rescue is not conditional, and the rollback proves what it restored.

    Everything that changes the runtime lives in one block, so a failure
    anywhere — the pull, the compose up, or any of the health assertions — is
    caught. A rollback that restarted the previous image and reported success
    without re-checking would be indistinguishable from an edge that is down.
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

    Docker has no restart-on-unhealthy behaviour. A comment that says otherwise
    invites the next reader to leave a wedged-but-running edge in place waiting
    for a restart that never comes; the assertion in health.yml is what turns
    that state into a failed deploy.
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
    """The probe moved into Compose; the tool it runs stays in the image.

    An image-level HEALTHCHECK would have to hard-code the status endpoint,
    which the deployment owns. A helper script would be a third place to look
    for what "healthy" means. curl is neither — it is the runtime dependency
    the Compose probe execs, and removing it would report every edge unhealthy.
    """
    dockerfile = (PROJECT_DIR / "docker/edge/Dockerfile").read_text(encoding="utf-8")

    assert not (PROJECT_DIR / ("healthcheck" + ".sh")).exists()
    assert not (PROJECT_DIR / "docker/edge" / ("healthcheck" + ".sh")).exists()
    # The instruction, not the word: the comment explaining its absence stays.
    assert not any(line.startswith("HEALTHCHECK") for line in dockerfile.splitlines())
    assert "healthcheck" + ".sh" not in dockerfile
    assert "blitzecdn-" + "healthcheck" not in dockerfile
    # In the runtime stage, not just the throwaway builder that also uses it.
    assert "curl" in dockerfile.split("FROM ${NGINX_IMAGE}")[-1]

    # Nothing anywhere else still reaches for the script or the environment
    # variable that pointed it at a URL — see
    # test_removed_host_compatibility_contracts_do_not_reappear, which walks
    # the tree for both names.
    assert "BLITZECDN_" + "HEALTH_URL" not in COMPOSE_TEMPLATE


# ----------------------------------------------------------------------
# Cross-role agreements
#
# blitzecdn_cache and blitzecdn_stats each recompute or re-read something
# blitzecdn_nginx configured. Nothing at run time can tell a disagreement from
# an ordinary empty result: a purge aimed at the wrong directory reports
# success having deleted nothing, and a reader aimed at the wrong log reports a
# hit ratio of zero, which looks like a broken cache rather than a broken
# reader. These are the only guards.
# ----------------------------------------------------------------------

CACHE_ROLE_DIR = _role("blitzecdn_cache")
STATS_ROLE_DIR = _role("blitzecdn_stats")


def _defaults_of(role_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((role_dir / "defaults/main.yml").read_text(encoding="utf-8"))


def _contract_value(*path: str) -> Any:
    value: Any = _runtime_defaults()["blitzecdn_edge_runtime"]
    for key in path:
        value = value[key]
    return value


@pytest.mark.parametrize(
    ("cache_key", "expected"),
    [
        ("blitzecdn_cache_path", ("paths", "cache")),
        ("blitzecdn_cache_purge_http_ports", ("listeners", "http")),
        ("blitzecdn_cache_purge_https_ports", ("listeners", "https")),
    ],
)
def test_purge_role_agrees_with_the_runtime_contract(cache_key, expected):
    """A purge computes file paths from these; a mismatch purges nothing.

    blitzecdn_cache runs in its own play — `blitzecdn cache purge` converges
    nothing else — so it keeps its own defaults rather than reading the
    contract. That leaves these literals as the only guard, and they are
    compared against the contract because the contract is what the edge was
    built from.
    """
    assert _defaults_of(CACHE_ROLE_DIR)[cache_key] == _contract_value(*expected), (
        f"{cache_key} in blitzecdn_cache disagrees with blitzecdn_edge_runtime."
        f"{'.'.join(expected)}. Purge would delete paths nginx never wrote to "
        "and report success."
    )


def test_purge_role_agrees_with_the_nginx_role():
    """Encoding normalization is Nginx policy, so it is compared to that role."""
    assert (
        _defaults_of(CACHE_ROLE_DIR)["blitzecdn_cache_normalize_accept_encoding"]
        == _role_defaults()["blitzecdn_nginx_normalize_accept_encoding"]
    )


def test_stats_role_reads_the_log_the_nginx_role_writes():
    stats = _defaults_of(STATS_ROLE_DIR)
    assert (
        stats["blitzecdn_stats_access_log_path"]
        == _role_defaults()["blitzecdn_nginx_access_log_path"]
    )
    assert stats["blitzecdn_stats_status_address"] == _contract_value(
        "status", "address"
    )
    assert stats["blitzecdn_stats_status_port"] == _contract_value("status", "port")
    assert stats["blitzecdn_stats_status_path"] == _contract_value("status", "path")


def test_purge_role_only_claims_the_cache_layout_the_nginx_role_emits():
    """The role computes <md5[-1]>/<md5[-3:-1]>/<md5>, which is levels=1:2 only.

    If the nginx template ever emits different levels, the purge role must stop
    claiming it can compute paths rather than silently deleting wrong ones.
    """
    template = (ROLE_DIR / "templates/cache.conf.j2").read_text(encoding="utf-8")
    assert "levels=1:2" in template
    spec = yaml.safe_load(
        (CACHE_ROLE_DIR / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )["argument_specs"]["main"]["options"]
    assert spec["blitzecdn_cache_levels"]["choices"] == ["1:2"]


def test_purge_covers_every_cache_key_variant_the_site_template_can_produce():
    """One URL is several entries, and leaving one behind still serves it.

    The site template puts the listener port, request method and normalized
    encoding in the key, so the purge role has to sweep all three dimensions.
    nginx caches GET and HEAD by default and the template does not narrow
    proxy_cache_methods.
    """
    site_template = (ROLE_DIR / "templates/site.conf.j2").read_text(encoding="utf-8")
    assert "$scheme$server_port$request_method$host{{ cache_uri }}" in site_template
    assert "proxy_cache_methods" not in site_template

    defaults = _defaults_of(CACHE_ROLE_DIR)
    assert set(defaults["blitzecdn_cache_purge_methods"]) == {"GET", "HEAD"}

    cache_conf = (ROLE_DIR / "templates/cache.conf.j2").read_text(encoding="utf-8")
    mapped = set(re.findall(r'"(br|gzip)"\s*;', cache_conf)) | {""}
    assert set(defaults["blitzecdn_cache_purge_encodings"]) == mapped, (
        "the encodings blitzecdn_cache purges differ from the ones the "
        "$blitzecdn_accept_encoding map can produce; the unlisted variant "
        "would survive a purge and keep being served"
    )


def test_named_purge_computes_the_same_port_cache_entry(tmp_path):
    """Execute the role's key calculation, including its Jinja loops."""
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")

    cache_path = tmp_path / "cache"
    key = "http80GETexample.com/asset"
    digest = hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()
    cached = cache_path / digest[-1] / digest[-3:-1] / digest
    computed = tmp_path / "computed.json"

    variables = _defaults_of(CACHE_ROLE_DIR) | {
        "blitzecdn_cache_path": str(cache_path),
        "blitzecdn_cache_purge_entries": [
            {"host": "example.com", "uri": "/asset", "scheme": "http"}
        ],
        "blitzecdn_cache_purge_all": False,
    }
    role_tasks = yaml.safe_load(
        (CACHE_ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    )
    playbook = tmp_path / "purge-key.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": variables,
                    # Run through "Compute the cache files". The remaining
                    # tasks chown a manifest to root, which a unit test neither
                    # needs nor has permission to do.
                    "tasks": [
                        *role_tasks[:5],
                        {
                            "copy": {
                                "content": (
                                    "{{ blitzecdn_cache_purge_files | to_json }}"
                                ),
                                "dest": str(computed),
                                "mode": "0600",
                            }
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [ansible, "-i", "localhost,", "-c", "local", str(playbook)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert str(cached) in yaml.safe_load(computed.read_text(encoding="utf-8"))


def test_the_edge_lifecycle_order_holds():
    """Two orderings, each of which has cost an outage when it was wrong.

    The engine, the persistent state and the runtime image are prepared before
    the firewall, because opening the public ports on a host that cannot serve
    them advertises an edge that is not there. And the container starts only
    after blitzecdn_nginx has rendered the configuration and proved it loads,
    because a container started against an incomplete tree is an edge answering
    444 for every customer.
    """
    play = (PROJECT_DIR / "ansible/playbooks/edge.yml").read_text(encoding="utf-8")
    order = [
        "name: blitzecdn_docker",
        "tasks_from: prepare.yml",
        "tasks_from: verify-runtime.yml",
        "role: blitzecdn_firewall",
        "role: blitzecdn_nginx",
        "role: blitzecdn_edge_stack",
    ]
    positions = [play.index(marker) for marker in order]
    assert positions == sorted(positions), (
        "the edge lifecycle is out of order; expected " + " -> ".join(order)
    )
    # The stack role must be the last thing that touches the runtime, and the
    # security roles stay after it — an edge whose containers are all broken
    # still has to be reachable for Ansible to repair it.
    assert play.index("role: blitzecdn_edge_stack") < play.index("role: blitzecdn_sshd")


def test_the_fleet_rollout_starts_with_one_edge():
    """A canary batch, and `any_errors_fatal` to make it mean something.

    Both halves are load-bearing. Widening batches without the fatal flag would
    roll a broken image over the whole fleet one batch at a time; the fatal flag
    without a first batch of one would take a quarter of the fleet down before
    anything stopped. `max_fail_percentage` is deliberately absent — it states
    the same policy in a second dialect and invites a later reader to change one
    believing they changed the rule.
    """
    play = yaml.safe_load(
        (PROJECT_DIR / "ansible/playbooks/edge.yml").read_text(encoding="utf-8")
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

    Settings are refused when they carry a credential-shaped word, and "key"
    is one of them — so a name like `blitzecdn_edge_image_key` would be
    rejected by the store and the documented upgrade would not work.
    """
    from blitzecdn.core.validation import validate_setting_name

    for name in (
        "blitzecdn_edge_image",
        "blitzecdn_edge_image_tag",
        "blitzecdn_edge_image_digest",
        "blitzecdn_edge_stack_image_pull",
    ):
        assert validate_setting_name(name) == name


# ----------------------------------------------------------------------
# The shared edge runtime contract
#
# blitzecdn_nginx, blitzecdn_edge_stack and blitzecdn_firewall converge the
# same machine and need the same answers about it: where its files live, which
# ports it listens on, where its health can be read. They used to get those
# answers by reaching into each other — blitzecdn_edge_stack derived fifteen of
# its defaults from blitzecdn_nginx_*, and blitzecdn_firewall did not couple at
# all and kept a second literal copy of the port lists instead. The tests below
# hold the replacement in place: one contract, read by all three, and no
# sibling reads left to grow back.
# ----------------------------------------------------------------------

#: The roles that converge an edge and share its runtime.
EDGE_ROLES = ("blitzecdn_nginx", "blitzecdn_edge_stack", "blitzecdn_firewall")

#: The one reference across those roles that is not a contract member.
#:
#: blitzecdn_edge_stack overrides blitzecdn_nginx_config_test_image when it asks
#: that role to validate the running configuration against a *new* image. That
#: is a parameter passed to a task file, which is what an entry point is for —
#: not a read of another role's state — and blitzecdn_nginx declares it.
#:
#: blitzecdn_nginx_listeners_claimed goes the other way: the Nginx role decides
#: whether any server block claims the public ports and publishes the answer,
#: and health.yml reads that rather than re-deriving it from the site list. A
#: published output is a contract of its own; the defaults file is not.
SIBLING_EXCEPTIONS = {
    "blitzecdn_nginx_config_test_image",
    "blitzecdn_nginx_listeners_claimed",
}


def test_no_edge_role_reads_another_edge_roles_variables():
    """The coupling this contract replaced must not grow back.

    A sibling read is invisible in review and expensive in production: it makes
    the role that runs the container depend on the role that writes
    configuration, for reasons that have nothing to do with configuration, and
    it means a value can be changed in one place and silently disagree in
    another. Everything genuinely shared is blitzecdn_edge_runtime's.
    """
    for role in EDGE_ROLES:
        others = [f"{other}_" for other in EDGE_ROLES if other != role]
        for source in sorted((ROLES_DIR / role).rglob("*")):
            if source.suffix not in {".yml", ".j2"} or not source.is_file():
                continue
            document = source.read_text(encoding="utf-8")
            for line in document.splitlines():
                # Prose is where these roles explain themselves to each other,
                # and naming a sibling there is the point.
                if line.lstrip().startswith("#"):
                    continue
                for prefix in others:
                    for word in re.findall(rf"{re.escape(prefix)}[a-z0-9_]+", line):
                        assert word in SIBLING_EXCEPTIONS, (
                            f"{source.relative_to(PROJECT_DIR)} reads {word}, "
                            "which belongs to another edge role. Shared runtime "
                            "values are blitzecdn_edge_runtime's."
                        )


def test_every_edge_role_declares_the_contract_it_reads():
    """An undeclared contract is an undefined-variable error mid-converge.

    Declaring it makes a play that forgot blitzecdn_edge fail in argument
    validation, before the role has changed anything.
    """
    for role in EDGE_ROLES:
        spec = yaml.safe_load(
            (ROLES_DIR / role / "meta/argument_specs.yml").read_text(encoding="utf-8")
        )["argument_specs"]["main"]["options"]
        assert spec["blitzecdn_edge_runtime"]["required"] is True, role
        assert spec["blitzecdn_edge_runtime"]["type"] == "dict", role


def test_the_contract_holds_no_value_only_one_role_uses():
    """Every member has to be read by at least two of the three roles.

    Otherwise the contract becomes the place variables go to escape their
    owner, and "shared runtime" stops meaning anything. Nginx policy — cache
    sizing, ciphers, compression — stays in blitzecdn_nginx; the health timeout
    and the rollback record stay in blitzecdn_edge_stack.
    """
    runtime = _runtime_defaults()["blitzecdn_edge_runtime"]
    sources = {
        role: "\n".join(
            source.read_text(encoding="utf-8")
            for source in sorted((ROLES_DIR / role).rglob("*"))
            if source.suffix in {".yml", ".j2"} and source.is_file()
        )
        for role in EDGE_ROLES
    }

    def members(prefix: str, value: Any):
        if isinstance(value, dict):
            for key, item in value.items():
                yield from members(f"{prefix}.{key}", item)
        else:
            yield prefix

    for member in members("blitzecdn_edge_runtime", runtime):
        readers = [role for role, text in sources.items() if member in text]
        assert len(readers) >= 2, (
            f"{member} is read only by {readers or 'nothing'}. A value one role "
            "owns belongs in that role's defaults, not in the shared contract."
        )


def test_the_shared_runtime_is_defined_in_exactly_one_place():
    """One authoritative value, which is the whole point of the exercise.

    The literals below used to appear in two and three role defaults at once,
    held together by tests asserting the copies agreed. Agreement is not
    something a single definition can fail at.
    """
    duplicated = (
        "/var/cache/nginx/blitzecdn",
        "/var/lib/blitzecdn/acme",
        "/var/lib/blitzecdn/empty",
        "/stub_status",
        "8090",
        "2052, 2082, 2086, 2095",
        "2053, 2083, 2087, 2096",
    )
    for role in EDGE_ROLES:
        defaults = (ROLES_DIR / role / "defaults/main.yml").read_text(encoding="utf-8")
        body = "\n".join(
            line for line in defaults.splitlines() if not line.lstrip().startswith("#")
        )
        for literal in duplicated:
            assert literal not in body, (
                f"{role} restates {literal!r}, which blitzecdn_edge owns. Two "
                "copies of a runtime value agree until the day one is changed."
            )


def _run_contract(
    tmp_path: Path, contract: dict[str, Any] | None = None, **inputs: Any
):
    """Execute the contract role, which is where its invariants live.

    Reading the assertions as data would prove they are written, not that they
    fire: a `when:` or an expression that raises at run time passes
    --syntax-check and ansible-lint alike.
    """
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")
    variables: dict[str, Any] = dict(inputs)
    if contract is not None:
        variables["blitzecdn_edge_runtime"] = contract
    ansible_local = tmp_path / "ansible-local"
    ansible_local.mkdir(exist_ok=True)
    playbook = tmp_path / "contract.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "vars": variables,
                    "roles": ["blitzecdn_edge"],
                }
            ]
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [ansible, "-i", "localhost,", "-c", "local", str(playbook)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("COV_CORE", "COVERAGE"))
        }
        | {
            "ANSIBLE_LOCALHOST_WARNING": "False",
            "ANSIBLE_LOCAL_TEMP": str(ansible_local),
            "ANSIBLE_ROLES_PATH": str(ROLES_DIR),
        },
        check=False,
    )


def test_the_shipped_contract_converges(tmp_path):
    """The defaults this collection ships have to pass their own validation."""
    result = _run_contract(tmp_path)
    assert result.returncode == 0, result.stdout


def test_the_shipped_contract_converges_with_http3_on(tmp_path):
    """HTTP/3 is the one input desired state writes on every deploy."""
    result = _run_contract(tmp_path, blitzecdn_edge_http3_enabled=True)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param(
            {"listeners": {"http": [], "https": [443], "http3": False}},
            "no HTTP listeners",
            id="no-http-listeners",
        ),
        pytest.param(
            {"listeners": {"http": [80], "https": [], "http3": False}},
            "no HTTPS listeners",
            id="no-https-listeners",
        ),
        pytest.param(
            {"listeners": {"http": [80, 8443], "https": [443, 8443], "http3": False}},
            "both the HTTP and the HTTPS listener set",
            id="port-in-both-sets",
        ),
        pytest.param(
            {"listeners": {"http": [80], "https": [8443], "http3": True}},
            "443 is not an HTTPS listener",
            id="http3-without-443",
        ),
        pytest.param(
            {"status": {"address": "127.0.0.1", "port": 443, "path": "/stub_status"}},
            "not a usable loopback endpoint",
            id="status-on-a-public-listener",
        ),
        pytest.param(
            {"status": {"address": "127.0.0.1", "port": 8090, "path": "stub_status"}},
            "not a usable loopback endpoint",
            id="status-path-without-a-slash",
        ),
        pytest.param(
            {"paths": {"nginx": "etc/nginx"}},
            "is not an absolute path",
            id="relative-path",
        ),
    ],
)
def test_the_contract_refuses_an_edge_that_could_not_serve(tmp_path, change, message):
    """Each of these is well-typed, passes the argument spec, and cannot serve.

    Which is why they are assertions rather than spec entries: the argument
    spec checks shape, and these are relationships between values.
    """
    contract = _runtime_defaults()["blitzecdn_edge_runtime"]
    contract = {
        key: (value | change[key] if key in change else value)
        if isinstance(value, dict)
        else value
        for key, value in contract.items()
    }

    result = _run_contract(tmp_path, contract)

    assert result.returncode != 0, result.stdout
    assert message in result.stdout, result.stdout


def test_the_firewall_opens_exactly_the_listeners_the_contract_declares(tmp_path):
    """Executed, not read: this is the rule set ufw is actually handed.

    A listener with no rule is an unreachable port and a rule with no listener
    is an open port that can never serve. Both roles read one contract member
    now, so the failure this guards is a rendering mistake rather than a
    disagreement — the port list reaching ufw has to be the contract's, in full,
    with UDP/443 present exactly when HTTP/3 is on.
    """
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")

    firewall = _role("blitzecdn_firewall")
    tasks = yaml.safe_load((firewall / "tasks/main.yml").read_text(encoding="utf-8"))
    compose = next(
        task
        for task in tasks[0]["block"]
        if task["name"] == "Compose the rule set this role manages"
    )

    def rules(http3: bool) -> set[str]:
        runtime = _runtime_defaults(blitzecdn_edge_http3_enabled=http3)
        computed = tmp_path / f"rules-{http3}.json"
        playbook = tmp_path / f"rules-{http3}.yml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    {
                        "hosts": "localhost",
                        "connection": "local",
                        "gather_facts": False,
                        "vars": _defaults_of(firewall)
                        | {
                            "blitzecdn_edge_runtime": runtime["blitzecdn_edge_runtime"],
                            "blitzecdn_firewall_ssh_port": 22,
                            "blitzecdn_firewall_ssh_sources": ["198.51.100.0/24"],
                        },
                        "tasks": [
                            compose,
                            {
                                "copy": {
                                    "content": (
                                        "{{ blitzecdn_firewall_desired_rules "
                                        "| to_json }}"
                                    ),
                                    "dest": str(computed),
                                    "mode": "0600",
                                }
                            },
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [ansible, "-i", "localhost,", "-c", "local", str(playbook)],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("COV_CORE", "COVERAGE"))
            }
            | {"ANSIBLE_LOCALHOST_WARNING": "False"},
            check=False,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        return set(yaml.safe_load(computed.read_text(encoding="utf-8")))

    listeners = _runtime_defaults()["blitzecdn_edge_runtime"]["listeners"]
    expected = {"tcp|22|198.51.100.0/24"} | {
        f"tcp|{port}|any" for port in listeners["http"] + listeners["https"]
    }

    assert rules(http3=False) == expected
    assert rules(http3=True) == expected | {"udp|443|any"}
