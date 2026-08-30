# ruff: noqa: F403,F405
from contract_support import *


def test_http3_uses_the_firewall_registry_for_udp_443():
    role = _role("blitzecdn_firewall")
    tasks = (role / "tasks/main.yml").read_text(encoding="utf-8")
    play = (PROJECT_DIR / "ansible/playbooks/edge.yml").read_text(encoding="utf-8")

    assert "['udp|443|any'] if blitzecdn_firewall_http3_enabled else []" in tasks
    assert "proto: udp" in tasks
    assert "when: blitzecdn_firewall_http3_enabled" in tasks
    assert "^(tcp|udp)\\|" in tasks
    assert "difference(blitzecdn_firewall_desired_rules)" in tasks
    assert play.index("tasks_from: install.yml") < play.index(
        "role: blitzecdn_firewall"
    )


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
    assert "blitzecdn_edge_stack_http3_enabled" in health
    assert "search(':443" in health


# ----------------------------------------------------------------------
# The containerised runtime
#
# Nginx runs in a container and its configuration does not. Everything below
# guards a seam between the two: an image that is validated but not the one
# that serves, a mount the configuration test cannot see, a persistent path
# nothing creates, a runtime removal that takes a customer's keys with it.
# ----------------------------------------------------------------------

COMPOSE_TEMPLATE = (STACK_ROLE_DIR / "templates/compose.yml.j2").read_text(
    encoding="utf-8"
)


def _ansible_jinja(**kwargs: Any) -> Any:
    """A Jinja environment with the handful of Ansible filters the edge uses.

    Rendering the real templates is the point: a compose file asserted on as
    text cannot tell a mount from a comment, and the mounts are what these
    tests are about.
    """
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined, **kwargs)
    environment.filters["dirname"] = os.path.dirname
    environment.filters["regex_replace"] = lambda value, pattern, replacement="": (
        re.sub(pattern, replacement, value)
    )
    environment.filters["bool"] = lambda value: (
        value
        if isinstance(value, bool)
        else str(value).strip().lower() in {"true", "yes", "on", "1"}
    )
    # `lookup('env', ...)` is Ansible's, not Jinja's. The two defaults that use
    # it are secrets read from the controller's environment and are none of
    # these tests' business.
    environment.globals["lookup"] = lambda *_args, **_kwargs: ""
    return environment


def _edge_context(**overrides: Any) -> dict[str, Any]:
    """The variables the edge stack renders from, with references resolved.

    Several of blitzecdn_edge_stack's defaults are deliberately references to
    blitzecdn_nginx's — one definition of where an edge keeps its cache, not two
    that agree until somebody changes one. Resolving them here is what lets
    these tests read the *paths*, which is what the mounts actually are.
    """
    context: dict[str, Any] = (
        _role_defaults() | _defaults_of(STACK_ROLE_DIR) | overrides
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


def _compose_mounts(**overrides: Any) -> dict[str, str]:
    """host path -> mode, for every bind mount the rendered edge service has."""
    context = _edge_context(**overrides)
    environment = _ansible_jinja(
        loader=jinja2.FileSystemLoader(STACK_ROLE_DIR / "templates"),
        keep_trailing_newline=True,
    )
    rendered = environment.get_template("compose.yml.j2").render(
        blitzecdn_edge_stack_resolved_image="example/edge@sha256:" + "ab" * 32,
        **context,
    )
    project = yaml.safe_load(rendered)
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
        for entry in _role_defaults()["blitzecdn_nginx_config_test_volumes"]
    }
    # Rendered with GeoIP on, because the probe mounts the database
    # unconditionally and this has to compare the full set. The other direction
    # is the safe one: a probe that always sees the database can never miss one
    # the edge has.
    compose = _compose_mounts(blitzecdn_edge_stack_geoip_enabled=True)
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
    writable = {path for path, mode in _compose_mounts().items() if mode == "rw"}
    assert writable == {
        _role_defaults()["blitzecdn_nginx_cache_path"],
        "/var/log/nginx",
    }


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
    context = _edge_context(blitzecdn_edge_stack_geoip_enabled=True)
    # A path may be named outright or reached through the variable that holds
    # it. Both count; what must not happen is neither.
    aliases: dict[str, set[str]] = {}
    for name, value in context.items():
        if isinstance(value, str) and value.startswith("/"):
            aliases.setdefault(value, set()).add(name)
    for path in _compose_mounts(blitzecdn_edge_stack_geoip_enabled=True):
        named = any(alias in created for alias in aliases.get(path, set()))
        assert path in created or named, (
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


def test_no_role_installs_a_native_nginx_stack():
    """The host keeps Linux, SSH, Docker, the firewall and sysctl. Not Nginx.

    A surviving apt task would put a second Nginx on the host competing for
    :80 with the container — the exact failure blitzecdn_edge_stack refuses to
    converge into.
    """
    native = {
        "nginx",
        "libnginx-mod-http-geoip2",
        "libnginx-mod-http-brotli-filter",
        "libnginx-mod-http-js",
        "geoipupdate",
    }
    for role in sorted(ROLES_DIR.iterdir()):
        if not role.is_dir() or role.name == "blitzecdn_edge_stack":
            continue
        for source in sorted(role.rglob("*.yml")):
            document = source.read_text(encoding="utf-8")
            if "ansible.builtin.apt:" not in document:
                continue
            for package in native:
                assert f"name: {package}\n" not in document, (
                    f"{source} installs {package}"
                )
    # The one role allowed to name them names them only to purge a legacy edge.
    migration = (STACK_ROLE_DIR / "tasks/native.yml").read_text(encoding="utf-8")
    assert "state: absent" in migration
    assert "purge: true" in migration


def test_the_edge_image_installs_the_abi_matched_stack_in_one_transaction():
    """Splitting the packages across layers is how an ABI mismatch gets built.

    Ubuntu pins every libnginx-mod-* package to an nginx-abi-<version> virtual
    package, so apt resolving all four at once is the guarantee. The build then
    proves it by loading all three modules and using a directive from each.
    """
    dockerfile = (PROJECT_DIR / "docker/edge/Dockerfile").read_text(encoding="utf-8")
    install = dockerfile[dockerfile.index("apt-get install") :]
    install = install[: install.index("rm -rf")]
    for package in (
        "nginx",
        "libnginx-mod-http-geoip2",
        "libnginx-mod-http-brotli-filter",
        "libnginx-mod-http-js",
    ):
        assert package in install, package
    assert "--with-http_v3_module" in dockerfile
    assert "module-probe.conf" in dockerfile

    probe = (PROJECT_DIR / "docker/edge/module-probe.conf").read_text(encoding="utf-8")
    for module in (
        "ngx_http_geoip2_module.so",
        "ngx_http_brotli_filter_module.so",
        "ngx_http_js_module.so",
    ):
        assert module in probe, module
    # A module that loads but registers no directive is indistinguishable from
    # a working one until an edge configuration uses it.
    assert "brotli off;" in probe
    assert "js_path" in probe


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


@pytest.mark.parametrize(
    ("cache_key", "nginx_key"),
    [
        ("blitzecdn_cache_path", "blitzecdn_nginx_cache_path"),
        (
            "blitzecdn_cache_normalize_accept_encoding",
            "blitzecdn_nginx_normalize_accept_encoding",
        ),
        ("blitzecdn_cache_purge_http_ports", "blitzecdn_nginx_http_ports"),
        ("blitzecdn_cache_purge_https_ports", "blitzecdn_nginx_https_ports"),
    ],
)
def test_purge_role_agrees_with_the_nginx_role(cache_key, nginx_key):
    """A purge computes file paths from these; a mismatch purges nothing."""
    assert _defaults_of(CACHE_ROLE_DIR)[cache_key] == _role_defaults()[nginx_key], (
        f"{cache_key} in blitzecdn_cache disagrees with {nginx_key} in "
        "blitzecdn_nginx. Purge would delete paths nginx never wrote to and "
        "report success."
    )


def test_stats_role_reads_the_log_the_nginx_role_writes():
    stats = _defaults_of(STATS_ROLE_DIR)
    nginx = _role_defaults()
    assert (
        stats["blitzecdn_stats_access_log_path"]
        == nginx["blitzecdn_nginx_access_log_path"]
    )
    assert (
        stats["blitzecdn_stats_status_address"]
        == nginx["blitzecdn_nginx_status_address"]
    )
    assert stats["blitzecdn_stats_status_port"] == nginx["blitzecdn_nginx_status_port"]
    assert stats["blitzecdn_stats_status_path"] == nginx["blitzecdn_nginx_status_path"]


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
        "tasks_from: install.yml",
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


def test_the_image_is_settable_as_ordinary_fleet_policy():
    """The README tells operators to roll out an image with `config set`.

    Settings are refused when they carry a credential-shaped word, and "key"
    is one of them — so a name like `blitzecdn_edge_image_key` would be
    rejected by the store and the documented upgrade would not work.
    """
    from blitzecdn.domain.validation import validate_setting_name

    for name in (
        "blitzecdn_edge_image",
        "blitzecdn_edge_image_tag",
        "blitzecdn_edge_image_digest",
        "blitzecdn_edge_stack_image_pull",
        "blitzecdn_edge_stack_migrate_from_native",
    ):
        assert validate_setting_name(name) == name
