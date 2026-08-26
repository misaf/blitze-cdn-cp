# ruff: noqa: F403,F405
from contract_support import *

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
