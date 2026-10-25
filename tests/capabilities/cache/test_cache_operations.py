import re

import pytest
from support.cache import purges
from support.control_plane import (
    FakeRunner,
    ansible_run,
    build_application,
    host_run,
    seed_site,
)

from blitzecdn.capabilities.cache.domain import PurgeEntry
from blitzecdn.capabilities.http.contract import HttpScheme
from blitzecdn.composition import Repository
from blitzecdn.core.domain.runs import RunStatus
from blitzecdn.core.exceptions import ConflictError, ExecutionError, NotFoundError

# ----------------------------------------------------------------------
# Cache purge
# ----------------------------------------------------------------------


def _purge_run():
    return ansible_run(host_run("edge-a", changed=1))


def _site(control, name="cdn", domain="example.com", **policy):
    """A site to purge, and the hostname that reaches it.

    Purge matches on ``server_names``, so the hostname is what matters.
    """
    slug = f"{'wildcard' if name == '*' else name}-{domain.replace('.', '-')}"
    return seed_site(control, name=slug, domain=domain, record=name, **policy)


def test_a_purge_reaches_the_edges_with_the_entries_it_was_given(settings):
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = build_application(settings=settings, repository=repository, runner=fake)
    _site(control)

    result = control.cache.purge_cache(
        "alice",
        entries=[
            PurgeEntry(host="cdn.example.com", uri="/app.js", scheme=HttpScheme.HTTP)
        ],
    )

    assert result.complete is True
    entries, purge_all, _ = purges(fake)[0]
    assert entries == (
        PurgeEntry(host="cdn.example.com", uri="/app.js", scheme=HttpScheme.HTTP),
    )
    assert purge_all is False


def test_a_purge_for_a_hostname_no_site_serves_is_refused(settings):
    """Otherwise it reports success having removed nothing."""
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = build_application(settings=settings, repository=repository, runner=fake)
    _site(control)

    with pytest.raises(NotFoundError, match=re.escape("other.example.com")):
        control.cache.purge_cache(
            "alice", entries=[PurgeEntry(host="other.example.com", uri="/x")]
        )
    assert purges(fake) == []


def test_a_purge_under_a_wildcard_site_is_allowed(settings):
    """nginx matches *.example.com to a.example.com, so purge must too."""
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = build_application(settings=settings, repository=repository, runner=fake)
    _site(control, name="*", domain="assets.example.com")

    result = control.cache.purge_cache(
        "alice",
        entries=[
            PurgeEntry(
                host="img.assets.example.com", uri="/a.png", scheme=HttpScheme.HTTP
            )
        ],
    )
    assert result.complete is True


def test_a_purge_drops_the_query_when_the_site_ignores_it(settings):
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = build_application(settings=settings, repository=repository, runner=fake)
    _site(control, cache_query_string_mode="ignore")

    result = control.cache.purge_cache(
        "alice",
        entries=[
            PurgeEntry(
                host="cdn.example.com", uri="/app.js?v=2", scheme=HttpScheme.HTTP
            )
        ],
    )

    entries, _, _ = purges(fake)[0]
    assert entries == (
        PurgeEntry(host="cdn.example.com", uri="/app.js", scheme=HttpScheme.HTTP),
    )
    assert result.entries[0].uri == "/app.js"


def test_a_purge_for_a_scheme_the_site_never_serves_is_refused(settings):
    """The cache key starts with $scheme, so the two are different entries.

    Purging an unserved scheme would delete nothing while every edge reports
    success.
    """
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = build_application(settings=settings, repository=repository, runner=fake)
    _site(control)

    with pytest.raises(ConflictError, match="scheme"):
        control.cache.purge_cache(
            "alice",
            entries=[
                PurgeEntry(
                    host="cdn.example.com", uri="/app.js", scheme=HttpScheme.HTTPS
                )
            ],
        )
    assert purges(fake) == []


def test_a_purge_over_http_against_a_tls_site_is_refused(settings):
    """Port 80 only answers 301 for a TLS site, so it caches nothing."""
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = build_application(settings=settings, repository=repository, runner=fake)
    _site(
        control,
        name="tls",
        ssl_mode="flexible",
        certificate_mode="existing",
        certificate_path="/etc/ssl/certs/tls.pem",
        certificate_key_path="/etc/ssl/private/tls.key",
    )

    with pytest.raises(ConflictError, match="scheme"):
        control.cache.purge_cache(
            "alice",
            entries=[
                PurgeEntry(
                    host="tls.example.com", uri="/app.js", scheme=HttpScheme.HTTP
                )
            ],
        )
    assert purges(fake) == []


def test_a_purge_for_a_disabled_site_is_refused(settings):
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = build_application(settings=settings, repository=repository, runner=fake)
    _site(control, name="off", enabled=False)

    with pytest.raises(NotFoundError):
        control.cache.purge_cache(
            "alice", entries=[PurgeEntry(host="off.example.com", uri="/x")]
        )


def test_purging_everything_and_named_entries_at_once_is_refused(settings):
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings, repository=repository, runner=FakeRunner()
    )
    _site(control)

    with pytest.raises(ConflictError):
        control.cache.purge_cache(
            "alice",
            entries=[
                PurgeEntry(host="cdn.example.com", uri="/x", scheme=HttpScheme.HTTP)
            ],
            purge_all=True,
        )


def test_a_purge_with_nothing_to_do_is_refused(settings):
    control = build_application(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )
    with pytest.raises(ConflictError):
        control.cache.purge_cache("alice")


def test_purging_everything_needs_no_site_to_exist(settings):
    """--all is about the cache on disk, not about what is currently declared."""
    fake = FakeRunner([_purge_run()])
    control = build_application(
        settings=settings, repository=Repository(settings.database_path), runner=fake
    )

    result = control.cache.purge_cache("alice", purge_all=True)

    assert result.complete is True
    assert purges(fake)[0][1] is True


def test_a_partial_purge_is_reported_as_incomplete(settings):
    """Some edges dropped the object and some did not: clients see both."""
    repository = Repository(settings.database_path)
    partial = ansible_run(
        host_run("edge-a", changed=1), host_run("edge-b", ok=0, unreachable=1)
    )
    control = build_application(
        settings=settings, repository=repository, runner=FakeRunner([partial])
    )
    _site(control)

    result = control.cache.purge_cache(
        "alice",
        entries=[
            PurgeEntry(host="cdn.example.com", uri="/app.js", scheme=HttpScheme.HTTP)
        ],
    )

    assert result.complete is False
    assert [host.host for host in result.failed] == ["edge-b"]


def test_a_purge_no_edge_answered_is_an_error(settings):
    """Silence is not success: the object may still be served everywhere."""
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings,
        repository=repository,
        runner=FakeRunner([ansible_run(status=RunStatus.FAILED, return_code=1)]),
    )
    _site(control)

    with pytest.raises(ExecutionError, match="no edge reported"):
        control.cache.purge_cache(
            "alice",
            entries=[
                PurgeEntry(host="cdn.example.com", uri="/x", scheme=HttpScheme.HTTP)
            ],
        )


def test_a_purge_is_recorded_in_the_audit_trail(settings):
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings, repository=repository, runner=FakeRunner([_purge_run()])
    )
    _site(control)

    control.cache.purge_cache(
        "alice",
        entries=[
            PurgeEntry(host="cdn.example.com", uri="/app.js", scheme=HttpScheme.HTTP)
        ],
    )

    event = repository.audit_log.list_audit_events()[0]
    assert event.action == "cache.purged"
    assert event.details["complete"] is True
    assert event.details["entries"][0]["uri"] == "/app.js"
