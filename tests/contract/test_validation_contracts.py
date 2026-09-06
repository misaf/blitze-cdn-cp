# ruff: noqa: F403,F405
from contract_support import *

# ----------------------------------------------------------------------
# Executing the role's validation tasks
#
# Everything above reads the role as data or renders its templates. Neither
# evaluates a `when:` or an `assert`, and `--syntax-check` and ansible-lint do
# not either — a conditional that raises at run time passes all of them. That
# gap shipped a broken deploy once: `when: item.firewall is defined and
# item.firewall` parses, lints, and then fails on ansible-core 2.19+ because
# its result is a dict rather than a boolean.
#
# roles/blitzecdn_nginx/tasks/validate.yml holds every task that inspects
# desired state and refuses to proceed, and changes nothing on the host, so it
# can be run here against real model output.
# ----------------------------------------------------------------------

VALIDATE_TASKS = ROLE_DIR / "tasks/validate.yml"
NGINX_BUILD_INVARIANT_TASKS = ROLE_DIR / "tasks/build-invariant.yml"
NGINX_PROBE_INVARIANT_TASKS = ROLE_DIR / "tasks/probe-invariant.yml"
CONVERGE_TASKS = ROLE_DIR / "tasks/main.yml"


def _run_validation(sites: list[dict[str, Any]], tmp_path: Path, **overrides: Any):
    """Execute the role's validation tasks against localhost."""
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")
    # A contract input is applied before the contract is composed; anything
    # else is an ordinary variable override on top of it.
    inputs, plain = _split_runtime(overrides)
    variables = (
        _role_defaults(**inputs)
        | {
            "blitzecdn_nginx_sites": sites,
        }
        | plain
    )
    ansible_local = tmp_path / "ansible-local"
    ansible_local.mkdir(exist_ok=True)
    playbook = tmp_path / "validate.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": variables,
                    "tasks": [{"import_tasks": str(VALIDATE_TASKS)}],
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
        # pytest-cov's subprocess hook would otherwise make the Ansible
        # child write statement-only coverage beside this run's branch data,
        # which coverage refuses to combine.
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("COV_CORE", "COVERAGE"))
        }
        # Its own local temp: the ambient one is a single shared directory, and
        # these runs happen concurrently under xdist.
        | {
            "ANSIBLE_LOCALHOST_WARNING": "False",
            "ANSIBLE_LOCAL_TEMP": str(ansible_local),
        },
        check=False,
    )


def _run_nginx_build_capability(
    tmp_path: Path, configure_arguments: str, *, version: str = "1.27.0"
):
    """Execute the build invariant against a fabricated `nginx -V` banner.

    Nginx runs in a container now, so there is no binary on the host to fake.
    The invariant was split out of the probe for exactly this: the assertions
    read two variables, so a test can hand them the output of a build that does
    not exist rather than needing an engine, an image and a network.
    """
    return _run_nginx_invariant(
        tmp_path,
        NGINX_BUILD_INVARIANT_TASKS,
        {
            "blitzecdn_nginx_config_test_image": "example/edge:test",
            "blitzecdn_nginx_build_status": 0,
            "blitzecdn_nginx_build_output": (
                f"nginx version: nginx/{version}\n"
                f"configure arguments: {configure_arguments}\n"
            ),
        },
    )


def _run_nginx_probe_invariant(tmp_path: Path, probe: dict[str, Any]):
    """Execute the probe invariant against a fabricated module result.

    The result of a probe that never ran is the case worth covering, and it is
    the one no engine will produce on demand: the assertion exists because a
    module error leaves no `status` behind, and `default(1)` then reads that
    absence as an image with no nginx in it.
    """
    return _run_nginx_invariant(
        tmp_path,
        NGINX_PROBE_INVARIANT_TASKS,
        {
            "blitzecdn_nginx_config_test_image": "example/edge:test",
            "blitzecdn_nginx_build_probe": probe,
        },
    )


def _run_nginx_invariant(tmp_path: Path, tasks_file: Path, variables: dict[str, Any]):
    """Run one of the role's assertion-only task files against localhost."""
    ansible_local = tmp_path / "ansible-local"
    ansible_local.mkdir()
    playbook = tmp_path / f"{tasks_file.stem}-run.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": variables,
                    "tasks": [{"import_tasks": str(tasks_file)}],
                }
            ]
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [
            shutil.which("ansible-playbook")
            or str(PROJECT_DIR / ".venv/bin/ansible-playbook"),
            "-i",
            "localhost,",
            "-c",
            "local",
            str(playbook),
        ],
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
        },
        check=False,
    )


def test_nginx_invariant_accepts_a_capable_build(tmp_path):
    result = _run_nginx_build_capability(
        tmp_path, "--with-http_v3_module --with-http_ssl_module"
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_nginx_invariant_rejects_an_unsupported_build_clearly(tmp_path):
    result = _run_nginx_build_capability(
        tmp_path, "--with-http_ssl_module", version="not-a-version"
    )
    assert result.returncode != 0
    assert "does not contain a working nginx binary" in result.stdout


def test_nginx_probe_invariant_accepts_a_probe_that_ran(tmp_path):
    """Including one that ran and came back non-zero.

    A container that started and exited badly is build-invariant.yml's to
    judge, and it has the output to judge it with. This gate is only about
    whether there is a result at all.
    """
    result = _run_nginx_probe_invariant(tmp_path, {"status": 1, "container": {}})
    assert result.returncode == 0, result.stdout + result.stderr


def test_nginx_probe_invariant_separates_a_probe_that_never_ran(tmp_path):
    """The message must be about the probe, not about the image.

    `failed_when: false` on the probe means a module error arrives here as a
    result with no `status` in it, which `default(1)` would otherwise render as
    an image with no nginx binary — sending whoever reads the failure to
    inspect an image that was never opened.
    """
    result = _run_nginx_probe_invariant(
        tmp_path,
        {"failed": True, "msg": "Error starting container: OCI runtime create failed"},
    )
    assert result.returncode != 0
    assert "OCI runtime create failed" in result.stdout
    assert "it is the probe that failed" in result.stdout
    assert "does not contain a working nginx binary" not in result.stdout


def test_role_validation_tasks_actually_run(desired_state, tmp_path):
    """The regression guard for a conditional that only fails at run time."""
    result = _run_validation(desired_state["blitzecdn_nginx_sites"], tmp_path)
    assert result.returncode == 0, (
        "the role's validation tasks failed against real desired state:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_role_accepts_only_immutable_managed_certificate_destinations(
    desired_state, tmp_path
):
    fingerprint = "ab" * 32
    managed = dict(desired_state["blitzecdn_nginx_sites"][0]) | {
        "certificate_mode": "uploaded",
        "certificate_path": (
            f"/etc/blitzecdn/tls/cdn-example-com/fullchain-{fingerprint}.pem"
        ),
        "certificate_key_path": (
            f"/etc/blitzecdn/tls/cdn-example-com/privkey-{fingerprint}.pem"
        ),
        "certificate_source_path": "/controller/fullchain.pem",
        "certificate_key_source_path": "/controller/privkey.pem",
    }

    accepted = _run_validation([managed], tmp_path)
    assert accepted.returncode == 0, accepted.stdout

    managed["certificate_path"] = "/etc/blitzecdn/tls/cdn-example-com/fullchain.pem"
    rejected = _run_validation([managed], tmp_path)
    assert rejected.returncode != 0
    assert "Invalid CDN site" in rejected.stdout


def test_role_accepts_the_connecting_ip_header_without_geoip(desired_state, tmp_path):
    """It reads the connection, not a database, so no capability gates it.

    Kept in core although its GeoIP sibling moved: this asserts that
    `blitzecdn_nginx` renders the header *without* consulting anything an
    optional distribution provides, which is a statement about core's role and
    would be untestable from a package that may not be installed.
    """
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {
        "visitor_headers": {"connecting_ip": True, "ip_country": False}
    }

    result = _run_validation(sites, tmp_path)

    assert result.returncode == 0, result.stdout


def test_role_rejects_a_wildcard_on_an_ip_address(desired_state, tmp_path):
    """Both halves of this control have to refuse it, not just the controller.

    `*.192.0.2.1` satisfies the hostname shape check — every label of an IPv4
    literal is a valid DNS label — and nginx renders `server_name *.192.0.2.1`
    without complaint, then matches no request ever sent. The control plane
    refuses it now, and this is the redundancy: the value reaches a directive
    the role writes as root.
    """
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {"server_names": ["*.192.0.2.1"]}

    result = _run_validation(sites, tmp_path)

    assert result.returncode != 0
    assert "Invalid CDN site" in result.stdout


# Three suites that used to be here are not any more, and their absence is the
# point rather than a gap:
#
#   * that the role refuses country rules and the BZ-IPCountry header without
#     GeoIP, and that a MaxMind key never reaches an Nginx configuration —
#     both now in `packages/blitzecdn-geoip/tests/`, executed against that
#     distribution's own role;
#   * that the role refuses Under Attack Mode without the challenge capability,
#     and that the signed njs module is written 0640 under `no_log` — now in
#     `packages/blitzecdn-security/tests/`.
#
# They moved with the implementation. A capability's refusal is asserted by the
# capability that makes it, so uninstalling the distribution takes the rule,
# the role and the test away together.


# ----------------------------------------------------------------------
# Executing a converge task, not a validation one
#
# The three tasks below render the capability HTTP fragments, list what is on
# disk, and delete whatever is no longer wanted. Their correctness is entirely
# in the set arithmetic between the second and the third, which is invisible
# to everything that reads the role rather than running it: the task names are
# right, the filter chain parses, ansible-lint is happy, and the expression
# still produced a list of paths that matched nothing on disk — so the prune
# deleted every fragment the render had just written, on every converge, and
# `js_import` with them.
# ----------------------------------------------------------------------


def _run_capability_http_resources(
    tmp_path: Path, declared: list[str], present: list[str]
):
    """Run the render-then-prune tasks over a real directory.

    Extracted from the role's own `main.yml` by name rather than copied, so
    this executes the tasks that ship. `declared` is what the control plane
    says is installed; `present` is what an earlier converge left behind.
    """
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")

    nginx_dir = tmp_path / "nginx"
    (nginx_dir / "conf.d").mkdir(parents=True)
    for name in present:
        (nginx_dir / "conf.d" / name).write_text("# stale\n", encoding="utf-8")

    resources = []
    for name in declared:
        template = tmp_path / f"{name}.j2"
        template.write_text(f"# rendered from {name}\n", encoding="utf-8")
        resources.append(
            {
                "plugin": name.split("-")[0],
                "name": f"{name}.j2",
                "template": str(template),
            }
        )

    wanted = {
        "Render installed capability HTTP resources",
        "Find previously rendered capability HTTP resources",
        "Remove detached capability HTTP resources",
    }
    tasks = [
        task
        for task in yaml.safe_load(CONVERGE_TASKS.read_text(encoding="utf-8"))
        if task.get("name") in wanted
    ]
    assert len(tasks) == len(wanted), (
        f"the role no longer has all of {sorted(wanted)}; this test executes "
        "them by name and cannot silently stop covering them"
    )
    for task in tasks:
        # The render writes root:root, which a test process is not. Dropped
        # here and nowhere else: what has to be exercised verbatim is the
        # `dest` expression, because the prune's job is to agree with it, and
        # ownership has no bearing on which paths the two compute.
        task.get("ansible.builtin.template", {}).pop("owner", None)
        task.get("ansible.builtin.template", {}).pop("group", None)

    ansible_local = tmp_path / "ansible-local"
    ansible_local.mkdir()
    playbook = tmp_path / "capability-http-resources.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "blitzecdn_edge_runtime": {"paths": {"nginx": str(nginx_dir)}},
                        "blitzecdn_nginx_resources": {"http": resources},
                    },
                    "tasks": tasks,
                    # The tasks notify a handler that lives in the role. Named
                    # here so they can run verbatim rather than being edited.
                    "handlers": [
                        {
                            "name": "Validate and reload Nginx",
                            "ansible.builtin.debug": {"msg": "noop"},
                        }
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
        | {
            "ANSIBLE_LOCALHOST_WARNING": "False",
            "ANSIBLE_LOCAL_TEMP": str(ansible_local),
        },
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return sorted(path.name for path in (nginx_dir / "conf.d").iterdir())


def test_the_converge_keeps_the_capability_fragments_it_just_rendered(tmp_path):
    """The prune removed all of them, which nginx reported as a missing import.

    A detached capability's fragment has to go, or its directives outlive the
    distribution that owns them. Everything still installed has to stay, and
    that half was broken: the desired-path list was built with a backreference
    that arrives at `re.sub` escaped, so every entry was the literal
    `blitzecdn-plugin-\\1`, nothing on disk matched, and `rejectattr` rejected
    nothing. The edge lost the `js_import` for Under Attack Mode on every
    converge and refused its own configuration.
    """
    remaining = _run_capability_http_resources(
        tmp_path,
        declared=["cache-http.conf", "security-http.conf"],
        present=["blitzecdn-plugin-detached-http.conf"],
    )

    assert remaining == [
        "blitzecdn-plugin-cache-http.conf",
        "blitzecdn-plugin-security-http.conf",
    ], (
        "the converge did not leave exactly the fragments the installed "
        f"capabilities declare; conf.d holds {remaining}"
    )


def test_the_converge_removes_a_detached_capabilitys_fragment(tmp_path):
    """The other half, and the reason the prune exists at all."""
    remaining = _run_capability_http_resources(
        tmp_path,
        declared=[],
        present=[
            "blitzecdn-plugin-geoip-http.conf",
            "blitzecdn-plugin-security-http.conf",
        ],
    )

    assert remaining == [], (
        "a capability that is no longer installed kept its http fragment, so "
        f"its directives outlive it; conf.d holds {remaining}"
    )


def test_a_site_no_hostname_routes_to_never_reaches_the_edge(settings, tmp_path):
    """Removing a site's last DNS record must not wedge every later deploy.

    `serves_traffic` is the model's answer to this and it says exactly why: a
    site with no hostnames would render a `server` block with an empty
    `server_name`, which nginx reads as the default server for the listener, so
    the site with the least configuration behind it starts answering for every
    hostname nobody else claimed. `validate.yml` refuses the same shape from the
    other side.

    Both halves existed; nothing joined them. The renderer published every site
    in the snapshot, so an operator who took the last hostname off a site — one
    `record remove`, no site deleted — left desired state carrying a site the
    role must reject, and *every* subsequent converge failed on it, for that
    edge and every other one in the deployment. The site is desired state; it is
    simply not yet something to serve.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(settings=settings, repository=repository)
    repository.zones.create_domain(Domain(name="example.com"))
    _seed_site(repository, name="cdn-example-com", label="cdn", origin="198.51.100.20")
    repository.sites.create_site(
        CdnSite.model_validate({"name": "awaiting-dns", "origin_host": "192.0.2.10"})
    )
    control.deployments.write_desired_state(
        repository.snapshot(), settings.generated_vars_path
    )
    published = yaml.safe_load(settings.generated_vars_path.read_text(encoding="utf-8"))

    names = [site["name"] for site in published["blitzecdn_nginx_sites"]]
    assert names == ["cdn-example-com"], (
        "a site with no hostnames was published to the edge; the role will "
        "refuse it and the deploy will fail until someone deletes the site"
    )
    result = _run_validation(published["blitzecdn_nginx_sites"], tmp_path)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
