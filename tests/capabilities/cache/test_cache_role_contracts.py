"""The agreements this capability's roles have with the edge core converges.

`blitzecdn_cache` and `blitzecdn_cache_stats` recompute or re-read what
`blitzecdn_nginx` configured, and a disagreement looks like an empty result: a
purge deleting nothing, or a hit ratio of zero. These are the only guards.

This side is read through :data:`blitzecdn.capabilities.cache.ansible.ROLES_PATH`,
the path a deployment resolves the roles by.
"""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from support.contracts import PROJECT_DIR, _role_defaults, _runtime_defaults

from blitzecdn.capabilities.cache import ansible

CACHE_ROLE_DIR = ansible.ROLES_PATH / "blitzecdn_cache_purge"
CONFIG_ROLE_DIR = ansible.ROLES_PATH / "blitzecdn_cache_config"
STATS_ROLE_DIR = ansible.ROLES_PATH / "blitzecdn_cache_stats"
NGINX_DIR = ansible.ROLES_PATH.parent.parent / "nginx"


def test_the_roles_this_capability_ships_are_where_it_says_they_are():
    """The contribution is only true if the directory really carries the roles."""
    assert ansible.ROLES_PATH.is_dir()
    assert sorted(path.name for path in ansible.ROLES_PATH.iterdir()) == [
        "blitzecdn_cache_config",
        "blitzecdn_cache_purge",
        "blitzecdn_cache_stats",
    ]
    assert (CACHE_ROLE_DIR / "tasks/main.yml").is_file()
    assert (STATS_ROLE_DIR / "files/collect-cache-stats.sh").is_file()


def test_the_plays_this_capability_runs_ship_with_it():
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
        ("blitzecdn_cache_purge_path", ("paths", "cache")),
        ("blitzecdn_cache_purge_http_ports", ("listeners", "http")),
        ("blitzecdn_cache_purge_https_ports", ("listeners", "https")),
    ],
)
def test_purge_role_agrees_with_the_runtime_contract(cache_key, expected):
    """A purge computes file paths from these; a mismatch purges nothing.

    blitzecdn_cache runs in its own play, so it keeps its own defaults; they are
    compared against the contract the edge was built from.
    """
    assert _defaults_of(CACHE_ROLE_DIR)[cache_key] == _contract_value(*expected), (
        f"{cache_key} in blitzecdn_cache disagrees with blitzecdn_edge_runtime."
        f"{'.'.join(expected)}. Purge would delete paths nginx never wrote to "
        "and report success."
    )


def test_purge_role_agrees_with_the_capability_owned_nginx_resource():
    """The cache package owns both normalization and every consumer of it."""
    template = (NGINX_DIR / "cache-http.conf.j2").read_text(encoding="utf-8")
    assert _defaults_of(CACHE_ROLE_DIR)[
        "blitzecdn_cache_purge_normalize_accept_encoding"
    ]
    assert "$blitzecdn_accept_encoding" in template


def test_stats_role_reads_the_log_the_nginx_role_writes():
    stats = _defaults_of(STATS_ROLE_DIR)
    assert (
        stats["blitzecdn_cache_stats_access_log_path"]
        == _role_defaults()["blitzecdn_nginx_access_log_path"]
    )
    assert stats["blitzecdn_cache_stats_status_address"] == _contract_value(
        "status", "address"
    )
    assert stats["blitzecdn_cache_stats_status_port"] == _contract_value(
        "status", "port"
    )
    assert stats["blitzecdn_cache_stats_status_path"] == _contract_value(
        "status", "path"
    )


def test_purge_role_only_claims_the_cache_layout_the_nginx_role_emits():
    """The role computes <md5[-1]>/<md5[-3:-1]>/<md5>, which is levels=1:2 only."""
    template = (NGINX_DIR / "cache-http.conf.j2").read_text(encoding="utf-8")
    assert "levels=1:2" in template
    spec = yaml.safe_load(
        (CACHE_ROLE_DIR / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )["argument_specs"]["main"]["options"]
    assert spec["blitzecdn_cache_purge_levels"]["choices"] == ["1:2"]


def test_purge_covers_every_cache_key_variant_the_site_template_can_produce():
    """One URL is several entries, and leaving one behind still serves it.

    The key includes the listener port, request method and normalized encoding,
    so the purge sweeps all three. nginx caches GET and HEAD by default.
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
        "blitzecdn_cache_purge_path": str(cache_path),
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
                    # Run through "Compute the cache files"; the rest chown a manifest
                    # to root.
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
    result = subprocess.run(  # noqa: S603 - fixed ansible-playbook argv over tmp_path
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

    The control plane looks for one fact name; a rename would make every
    collection come back empty while every edge reports success.
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
        "blitzecdn_cache_stats must publish its document as the blitzecdn_report "
        "fact consumed by the Runner event adapter"
    )


def test_nothing_private_is_cached_or_served_from_cache():
    """The exclusions a shared cache has to get right, asserted one by one.

    Three are nginx defaults that only `proxy_ignore_headers` would switch off, so
    its absence is checked:

    * a response carrying `Set-Cookie` is not cached;
    * a response carrying `Cache-Control: private`, `no-cache` or `no-store`, or
      an `Expires` in the past, is not cached;
    * only GET and HEAD are cached, `proxy_cache_methods` being left at its
      default.

    The fourth was added: a response to an authorized request is neither stored
    nor served from cache. Both directives are needed: bypass alone still stores,
    and no_cache alone still serves an earlier entry.
    """
    template = (NGINX_DIR / "cache-upstream.conf.j2").read_text(encoding="utf-8")

    assert "proxy_cache_bypass $http_upgrade $http_authorization;" in template
    assert "proxy_no_cache $http_upgrade $http_authorization;" in template
    # None of this capability's nginx fragments may tell nginx to
    # disregard what the origin said about cacheability.
    for fragment in sorted(NGINX_DIR.glob("*.conf.j2")):
        assert "proxy_ignore_headers" not in fragment.read_text(encoding="utf-8"), (
            f"{fragment.name} would switch off nginx's own Set-Cookie and "
            "Cache-Control handling, which is what keeps private responses out "
            "of a shared cache"
        )
    assert "proxy_cache_methods" not in template
