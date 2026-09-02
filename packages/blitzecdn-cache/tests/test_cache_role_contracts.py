"""The agreements this capability's roles have with the edge core converges.

These moved here with the roles. `blitzecdn_cache` and `blitzecdn_stats` each
recompute or re-read something `blitzecdn_nginx` configured, and nothing at run
time can tell a disagreement from an ordinary empty result: a purge aimed at
the wrong directory reports success having deleted nothing, and a reader aimed
at the wrong log reports a hit ratio of zero, which looks like a broken cache
rather than a broken reader. These are the only guards.

Core's side is read through the shared contract helpers; this package's side is
read through :data:`blitzecdn_cache.ansible.ROLES_PATH` — the same path a
deployment resolves the roles by, rather than a directory in the checkout that
an installed wheel would not have.
"""

# ruff: noqa: F403,F405
from blitzecdn_cache import ansible
from contract_support import *

CACHE_ROLE_DIR = ansible.ROLES_PATH / "blitzecdn_cache"
CONFIG_ROLE_DIR = ansible.ROLES_PATH / "blitzecdn_cache_config"
STATS_ROLE_DIR = ansible.ROLES_PATH / "blitzecdn_stats"
NGINX_DIR = Path(__file__).parents[1] / "src/blitzecdn_cache/nginx"


def test_the_roles_this_distribution_ships_are_where_it_says_they_are():
    """The contribution is only true if the directory really carries the roles.

    Everything below reads these two directories, so a wheel built without its
    Ansible tree would make the rest of this file fail with `FileNotFoundError`
    rather than with the thing it is actually about.
    """
    assert ansible.ROLES_PATH.is_dir()
    assert sorted(path.name for path in ansible.ROLES_PATH.iterdir()) == [
        "blitzecdn_cache",
        "blitzecdn_cache_config",
        "blitzecdn_stats",
    ]
    assert (CACHE_ROLE_DIR / "tasks/main.yml").is_file()
    assert (STATS_ROLE_DIR / "files/collect-cache-stats.sh").is_file()


def test_the_plays_this_distribution_runs_ship_with_it():
    assert ansible.CACHE_PURGE_PLAYBOOK.is_file()
    assert ansible.STATS_PLAYBOOK.is_file()


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


def test_purge_role_agrees_with_the_package_owned_nginx_resource():
    """The cache package owns both normalization and every consumer of it."""
    template = (NGINX_DIR / "cache-http.conf.j2").read_text(encoding="utf-8")
    assert _defaults_of(CACHE_ROLE_DIR)["blitzecdn_cache_normalize_accept_encoding"]
    assert "$blitzecdn_accept_encoding" in template


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
    template = (NGINX_DIR / "cache-http.conf.j2").read_text(encoding="utf-8")
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
    site_template = (NGINX_DIR / "cache-upstream.conf.j2").read_text(encoding="utf-8")
    assert "$scheme$server_port$request_method$host{{ cache_uri }}" in site_template
    assert "proxy_cache_methods" not in site_template

    defaults = _defaults_of(CACHE_ROLE_DIR)
    assert set(defaults["blitzecdn_cache_purge_methods"]) == {"GET", "HEAD"}

    cache_conf = (NGINX_DIR / "cache-http.conf.j2").read_text(encoding="utf-8")
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


def test_the_stats_role_publishes_through_the_agreed_report_fact():
    """The channel a role returns a payload on is a contract like any other.

    `blitzecdn_stats` is the only role that returns data rather than an
    outcome, and the control plane finds it by looking for one fact name. If
    the role renamed it, every collection would come back empty with every edge
    reporting success — the silent-no-op failure this suite exists to catch.
    """
    tasks = (STATS_ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    adapter = (PROJECT_DIR / "src/blitzecdn/core/ansible/events.py").read_text(
        encoding="utf-8"
    )

    assert 'get("blitzecdn_report")' in adapter, (
        "the Runner event adapter no longer collects blitzecdn_report; the stats "
        "role publishes it and nothing else would carry the counters back"
    )
    assert "blitzecdn_report:" in tasks, (
        "blitzecdn_stats must publish its document as the blitzecdn_report "
        "fact consumed by the Runner event adapter"
    )


def test_the_stats_role_no_longer_wants_a_controller_directory():
    """Its report travels with the run, so there is no path to hand it.

    A resurrected `blitzecdn_stats_output_dir` would be a required option the
    control plane never sets, and role argument validation would fail every
    collection.
    """
    spec = yaml.safe_load(
        (STATS_ROLE_DIR / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )["argument_specs"]["main"]["options"]

    assert "blitzecdn_stats_output_dir" not in spec
