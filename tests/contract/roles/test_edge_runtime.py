"""The containerised runtime.

Nginx runs in a container and its configuration does not. Everything here
guards a seam between the two: an image that is validated but not the one that
serves, a mount the configuration test cannot see, a persistent path nothing
creates, a runtime removal that takes a customer's keys with it.
"""

# ruff: noqa: F403,F405

from contract_support import *
from role_contract_support import (
    COMPOSE_TEMPLATE,
    _defaults_of,
    _edge_context,
    _render_compose,
)

from blitzecdn.docker import (
    EDGE_DOCKERFILE,
    EDGE_MODULE_PROBE_CONF,
)


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
            ROLE_DIR / "tasks/main.yml",
        )
    )
    context = _edge_context()
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

    for path in _compose_mounts():
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
    teardown = _role("blitzecdn_edge_teardown")
    tasks = yaml.safe_load((teardown / "tasks/main.yml").read_text(encoding="utf-8"))
    assert _defaults_of(teardown)["blitzecdn_edge_teardown_remove_data"] is True

    def gate(name: str) -> str:
        return str(next(task for task in tasks if task["name"] == name).get("when", ""))

    # Which switch, not how it is spelled. The coercion these conditionals
    # carry is `test_no_conditional_trusts_a_boolean_it_was_handed_as_a_string`
    # to enforce; pinning the exact string here made adding it look like a
    # change of meaning.
    switch = "blitzecdn_edge_teardown_remove_data"
    assert switch not in gate("Stop and remove the edge stack"), (
        "stopping the edge is gated on the switch that erases it, so a host "
        "cannot be taken out of service without being destroyed"
    )
    for destructive in (
        "Remove the ACME webroot",
        "Remove cached responses",
        "Remove controller-written edge state and TLS material",
        "Remove the managed-site registry",
    ):
        assert switch in gate(destructive), destructive


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
    dockerfile = EDGE_DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG NGINX_IMAGE=nginx:1.31.4-alpine" in dockerfile
    assert dockerfile.count("FROM ${NGINX_IMAGE}") == 2
    assert '"${NGINX_VERSION}-${PKG_RELEASE}"' in dockerfile
    # Every package the builder stage produced has to carry this image's Nginx
    # version in its filename. It replaces two `apk add` lines that named the
    # two modules, and it is the same ABI guarantee without the enumeration.
    assert "was not built for nginx ${NGINX_VERSION}" in dockerfile
    assert "--with-http_v3_module" in dockerfile
    assert "module-probe.conf" in dockerfile
    assert "include /etc/nginx/modules.conf;" in dockerfile
    assert "include /etc/nginx/sites-enabled/" in dockerfile

    probe = EDGE_MODULE_PROBE_CONF.read_text(encoding="utf-8")
    assert "include /etc/nginx/modules.conf;" in probe
    # A module that loads but registers no directive is indistinguishable from
    # a working one until an edge configuration uses it. The directives are
    # generated from what the capabilities declared, so the probe includes
    # them rather than spelling them.
    assert "include /usr/share/blitzecdn/module-probe-directives.conf;" in probe


def test_the_edge_image_enumerates_no_capability():
    """The image build takes its module set as an input, not as a literal.

    This is the regression the whole mechanism exists to refuse. A module name
    written into the build context is a second register of which capabilities
    exist, and the one that cannot be corrected by detaching a distribution:
    the image is built once and pinned by digest, so an edge whose controller
    has no `blitzecdn-geoip` installed still loaded GeoIP2 for as long as the
    Dockerfile said so.
    """
    for path in (EDGE_DOCKERFILE, EDGE_MODULE_PROBE_CONF):
        document = path.read_text(encoding="utf-8").lower()
        for capability in ("geoip", "brotli", "njs", "maxmind"):
            # The Dockerfile's prose may explain what moved and why; what it
            # may not do is name a capability where the build reads it.
            directives = [
                line
                for line in document.splitlines()
                if capability in line and not line.lstrip().startswith("#")
            ]
            assert directives == [], f"{path.name} names {capability}: {directives}"
    dockerfile = EDGE_DOCKERFILE.read_text(encoding="utf-8")
    for argument in ("ENABLED_MODULES", "LOADED_MODULES", "MODULE_PROBE_DIRECTIVES"):
        assert f"ARG {argument}" in dockerfile, argument

    build_probe = (
        STACK_ROLE_DIR.parent / "blitzecdn_nginx/tasks/build-capability.yml"
    ).read_text(encoding="utf-8")
    assert "read_only: true" in build_probe
    assert '"/run:rw,noexec,nosuid,size=8m"' in build_probe

    config_test = (
        STACK_ROLE_DIR.parent / "blitzecdn_nginx/tasks/config-test.yml"
    ).read_text(encoding="utf-8")
    assert "network_mode: none" in config_test
    assert "read_only: true" in config_test
    assert "no-new-privileges:true" in config_test


def test_the_edge_image_deletes_nothing_it_inherited():
    """A whiteout is a layer that a nested or unprivileged engine cannot apply.

    Deleting a file that came from the base image records `.wh.<name>` in this
    layer, and applying that means a 0:0 character device or a `trusted.*`
    xattr — refused inside a nested engine. The failure is not local to the
    file: no container starts from the image at all, and the engine reports a
    layer it could not extract, which names neither the file nor the `rm` that
    put it there. `rm -f /etc/nginx/conf.d/default.conf` cost the HTTP/3 edge
    job a week of looking at the wrong thing.

    Emptying an inherited file is fine — that is an ordinary modified file — as
    is removing one this same layer created. Only the inherited ones matter, so
    a deliberate deletion of something written above it in the same `RUN` can
    relax this with its reason written down.
    """
    final_stage = EDGE_DOCKERFILE.read_text(encoding="utf-8").rsplit("\nFROM ", 1)[-1]
    deletions = [
        line.strip()
        for line in final_stage.splitlines()
        if not line.lstrip().startswith("#")
        and re.search(r"(^|[;&|]|\s)(rm|unlink)\s", line)
    ]
    assert deletions == [], (
        "the edge image's final stage deletes a path, which records a whiteout "
        f"a pre-seeded engine cannot apply: {deletions}"
    )


def test_nginx_logs_to_persistent_files_and_docker_streams():
    """Docker logs supplement the retained files consumed by edge tooling."""
    dockerfile = EDGE_DOCKERFILE.read_text(encoding="utf-8")
    site = (ROLE_DIR / "templates/site.conf.j2").read_text(encoding="utf-8")

    assert "error_log  /dev/stderr notice;" in dockerfile
    assert "access_log  /dev/stdout  main;" in dockerfile
    assert (
        site.count("access_log /dev/stdout {{ blitzecdn_nginx_log_format_name }};") == 2
    )
    assert "access_log {{ blitzecdn_nginx_access_log_path }}" in site
    assert 'max-size: "32m"' in COMPOSE_TEMPLATE
    assert 'max-file: "3"' in COMPOSE_TEMPLATE
