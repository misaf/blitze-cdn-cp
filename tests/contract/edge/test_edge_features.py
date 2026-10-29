"""Per-feature rendering: compression, cache key, uploads, websockets, TLS floor."""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from support.contracts import (
    role_defaults,
    runtime_defaults,
)
from support.edge_render import (
    ROLE_DIR,
    defaults_of,
    render_site,
    role_spec,
)
from support.paths import REPO_ROOT
from support.policy import site_from_settings

import blitzecdn
from blitzecdn.capabilities.cache.contract import CacheQueryStringMode
from blitzecdn.capabilities.compression.contract import CompressionMode
from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.http.contract import (
    HTTP_PROXY_PORTS,
    HTTPS_PROXY_PORTS,
    MaxUploadSize,
)
from blitzecdn.capabilities.tls.contract import (
    MinimumTlsVersion,
)

REQUIRES_CAPABILITIES = {
    "test_websocket_upgrade_is_forwarded_and_never_cached": ("cache",),
    "test_cache_query_string_mode_selects_the_cache_key": ("cache",),
    "test_brotli_uses_the_managed_filter_and_keeps_gzip_fallback": ("compression",),
    "test_compression_off_says_so_rather_than_staying_silent": ("compression",),
    "test_gzip_only_turns_the_managed_brotli_filter_off": ("compression",),
    "test_compression_leaves_the_cache_key_and_origin_request_alone": (
        "cache",
        "compression",
    ),
    "test_missing_compression_preserves_pre_upgrade_behavior": ("compression",),
}


def test_websocket_upgrade_is_forwarded_and_never_cached():
    """The handshake is forwarded, and neither stored nor served from cache.

    The cache directives name `$http_upgrade` first and `$http_authorization`
    after it. Asserted as a prefix so this stays about WebSocket upgrades; the
    cache capability's suite holds the full exclusion list.
    """
    site = site_from_settings(
        {
            "name": "socket",
            "server_names": ["socket.example.com"],
            "origin_host": "origin.example.com",
        }
    )
    rendered = render_site(site_to_ansible(site))
    http_template = (ROLE_DIR / "templates/http.conf.j2").read_text(encoding="utf-8")

    assert "map $http_upgrade $blitzecdn_connection_upgrade" in http_template
    assert "proxy_set_header Upgrade $http_upgrade;" in rendered
    assert "proxy_set_header Connection $blitzecdn_connection_upgrade;" in rendered
    assert "proxy_cache_bypass $http_upgrade" in rendered
    assert "proxy_no_cache $http_upgrade" in rendered


@pytest.mark.parametrize(
    ("minimum", "protocols"),
    [
        (MinimumTlsVersion.TLS_1_2, "TLSv1.2 TLSv1.3"),
        (MinimumTlsVersion.TLS_1_3, "TLSv1.3"),
    ],
)
def test_minimum_tls_version_renders_per_hostname(minimum, protocols):
    site = site_from_settings(
        {
            "name": "tls-minimum",
            "server_names": ["tls.example.com"],
            "origin_host": "origin.example.com",
            "ssl_mode": "flexible",
            "minimum_tls_version": minimum,
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
    )

    assert f"ssl_protocols {protocols};" in render_site(site_to_ansible(site))


@pytest.mark.parametrize(
    ("mode", "key_uri"),
    [
        (CacheQueryStringMode.INCLUDE, "$request_uri"),
        (CacheQueryStringMode.IGNORE, "$blitzecdn_uri_without_query"),
    ],
)
def test_cache_query_string_mode_selects_the_cache_key(mode, key_uri):
    site = site_from_settings(
        {
            "name": "query-mode",
            "server_names": ["query.example.com"],
            "origin_host": "origin.example.com",
            "cache_query_string_mode": mode,
        }
    )

    rendered = render_site(site_to_ansible(site))

    assert (
        f'proxy_cache_key "$scheme$server_port$request_method$host{key_uri}' in rendered
    )
    # Ignore affects identity in the cache, not what the origin receives.
    assert "proxy_pass $blitzecdn_upstream$request_uri;" in rendered


def _compressed(mode: CompressionMode) -> dict[str, Any]:
    return site_to_ansible(
        site_from_settings(
            {
                "name": "compressed",
                "server_names": ["compressed.example.com"],
                "origin_host": "origin.example.com",
                "compression": mode,
            }
        )
    )


def test_brotli_uses_the_managed_filter_and_keeps_gzip_fallback():
    site = _compressed(CompressionMode.BROTLI)
    rendered = render_site(site)
    assert "brotli on;" in rendered
    assert "brotli_comp_level 5;" in rendered
    assert "gzip on;" in rendered, "Brotli never replaces the gzip fallback"


def test_compression_off_says_so_rather_than_staying_silent():
    """Debian's nginx.conf carries `gzip on`, so omitting the directive means
    inherited, not off.
    """
    rendered = render_site(_compressed(CompressionMode.OFF))

    assert "gzip off;" in rendered
    assert "gzip on;" not in rendered


def test_gzip_only_turns_the_managed_brotli_filter_off():
    rendered = render_site(_compressed(CompressionMode.GZIP))

    assert "gzip on;" in rendered
    assert "brotli off;" in rendered
    assert "brotli on;" not in rendered


def test_compression_never_lists_text_html():
    """Both modules always compress it, and gzip warns about the duplicate.

    A warning on every `nginx -t` teaches operators to ignore the config test.
    """
    compression_role = (
        REPO_ROOT
        / "src/blitzecdn/capabilities/compression/ansible/roles"
        / "blitzecdn_compression"
    )
    types = defaults_of(compression_role)["blitzecdn_compression_types"]

    assert "text/html" not in types
    # Nothing already compressed: re-encoding one of these adds bytes and CPU.
    assert not {"image/jpeg", "image/png", "font/woff2", "application/zip"} & set(types)


def test_compression_leaves_the_cache_key_and_origin_request_alone():
    """The edge compresses on egress; the cache still stores what the origin sent.

    Otherwise a cache entry would carry an encoding the key does not distinguish.
    """
    rendered = render_site(_compressed(CompressionMode.BROTLI))

    assert "proxy_set_header Accept-Encoding $blitzecdn_accept_encoding;" in rendered
    assert (
        'proxy_cache_key "$scheme$server_port$request_method$host$request_uri'
        '$blitzecdn_accept_encoding"' in rendered
    )
    # gzip_vary is for shared caches downstream of us, which do not know our key.
    assert "gzip_vary on;" in rendered
    # Proxied responses carry headers that disable gzip under the default.
    assert "gzip_proxied any;" in rendered


def test_managed_nginx_stack_uses_ubuntu_abi_matched_modules():
    """The binary and its dynamic modules ship as one image.

    They are resolved, published and pulled together, so nothing on an edge can
    mismatch them; the edge names no packages at all.
    """
    assert "blitzecdn_nginx_packages" not in role_defaults()
    assert (
        "ghcr.io/misaf/blitzecdn-edge"
        in runtime_defaults()["blitzecdn_edge_runtime_image_default"]
    )


def test_the_pinned_edge_image_names_this_release():
    """The package and the edge image it deploys are one release.

    `.github/workflows/edge-image.yml` tags images with the `pyproject.toml`
    version, and a stale pin once left edges validating against the wrong Nginx
    build. Asserted rather than derived, so the pin is greppable and never names
    an unpublished image. `just release-version` moves them together.
    """
    version = blitzecdn.__version__
    assert runtime_defaults()["blitzecdn_edge_runtime_image_default"].endswith(
        f":{version}"
    ), "blitzecdn_edge/defaults/main.yml pins an edge image from another release"

    fleet = yaml.safe_load(
        (
            REPO_ROOT
            / "src/blitzecdn/assets/ansible"
            / "inventory/group_vars/blitzecdn_edges/defaults.yml"
        ).read_text()
    )
    assert fleet["blitzecdn_edge_image_tag"] == version, (
        "the fleet group_vars tag and the standalone fallback name different releases"
    )


def test_missing_compression_preserves_pre_upgrade_behavior():
    """With the setting absent, the role's default matches the domain's: brotli on."""
    site = _compressed(CompressionMode.BROTLI)
    del site["compression"]

    option = role_spec()["blitzecdn_nginx_sites"]["options"]["compression"]
    assert option["default"] == "brotli"
    assert option.get("required", False) is False

    assert "gzip on;" in render_site(site)


def _uploads(size: MaxUploadSize | None) -> dict[str, Any]:
    """A TLS site, so both the plain and the encrypted server block render."""
    document = site_to_ansible(
        site_from_settings(
            {
                "name": "uploads",
                "server_names": ["uploads.example.com"],
                "origin_host": "origin.example.com",
                "ssl_mode": "flexible",
                "certificate_mode": "existing",
                "certificate_path": "/etc/ssl/certs/edge.pem",
                "certificate_key_path": "/etc/ssl/private/edge.key",
                **({} if size is None else {"max_upload_size": size}),
            }
        )
    )
    if size is None:
        del document["max_upload_size"]
    return document


@pytest.mark.parametrize("size", list(MaxUploadSize))
def test_the_upload_limit_covers_every_listener_a_site_serves(size):
    """Server level, not location level.

    Inside `location /` alone, the ACME challenge, reserved `/.blitzecdn` paths
    and feature fragments would keep nginx's 1m default.
    """
    rendered = render_site(_uploads(size))
    blocks = rendered.split("server {")[1:]

    # One server block per public proxy port, plain and TLS alike.
    assert len(blocks) == len(HTTP_PROXY_PORTS) + len(HTTPS_PROXY_PORTS)
    assert rendered.count(f"client_max_body_size {size.value};") == len(blocks)
    for block in blocks:
        before, _, rest = block.partition(f"client_max_body_size {size.value};")
        assert rest, "every server block carries the limit"
        assert "location" not in before, "the limit precedes every location"


def test_missing_max_upload_size_preserves_pre_upgrade_behavior():
    """With the setting absent, the role's default matches the domain's."""
    option = role_spec()["blitzecdn_nginx_sites"]["options"]["max_upload_size"]
    assert option["default"] == MaxUploadSize.SMALL.value
    assert option.get("required", False) is False

    assert "client_max_body_size 100m;" in render_site(_uploads(None))
