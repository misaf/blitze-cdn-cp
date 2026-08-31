# ruff: noqa: F403,F405
from application_support import *
from blitzecdn_cache.composition import build_cache_service
from blitzecdn_cache.domain import PurgeEntry
from cache_support import purges

# ----------------------------------------------------------------------
# Cache purge
# ----------------------------------------------------------------------


def _purge_run():
    return ansible_run(host_run("edge-a", changed=1))


def _site(
    control,
    repository,
    name="cdn-example-com",
    server="cdn.example.com",
    **policy,
):
    repository.sites.create_site(
        CdnSite.model_validate(
            {
                "name": name,
                "server_names": [server],
                "origin_host": "o.example.com",
                **policy,
            }
        )
    )


def test_a_purge_reaches_the_edges_with_the_entries_it_was_given(settings):
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    _site(control, repository)

    result = build_cache_service(control).purge_cache(
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
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    _site(control, repository)

    with pytest.raises(NotFoundError, match=re.escape("other.example.com")):
        build_cache_service(control).purge_cache(
            "alice", entries=[PurgeEntry(host="other.example.com", uri="/x")]
        )
    assert purges(fake) == []


def test_a_purge_under_a_wildcard_site_is_allowed(settings):
    """nginx matches *.example.com to a.example.com, so purge must too."""
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    _site(control, repository, server="*.assets.example.com")

    result = build_cache_service(control).purge_cache(
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
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    _site(control, repository, cache_query_string_mode="ignore")

    result = build_cache_service(control).purge_cache(
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

    A site without TLS is served entirely over port 80, so nothing of it is
    stored under `https`. Purging that scheme computes a different MD5 and
    deletes a file that was never written, while every edge reports success.
    """
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    _site(control, repository)

    with pytest.raises(ConflictError, match="scheme"):
        build_cache_service(control).purge_cache(
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
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    repository.sites.create_site(
        CdnSite.model_validate(
            {
                "name": "tls-example-com",
                "server_names": ["tls.example.com"],
                "origin_host": "o.example.com",
                "ssl_mode": "flexible",
                "certificate_mode": "existing",
                "certificate_path": "/etc/ssl/certs/tls.pem",
                "certificate_key_path": "/etc/ssl/private/tls.key",
            }
        )
    )

    with pytest.raises(ConflictError, match="scheme"):
        build_cache_service(control).purge_cache(
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
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    repository.sites.create_site(
        CdnSite.model_validate(
            {
                "name": "off-example-com",
                "server_names": ["off.example.com"],
                "origin_host": "o.example.com",
                "enabled": False,
            }
        )
    )

    with pytest.raises(NotFoundError):
        build_cache_service(control).purge_cache(
            "alice", entries=[PurgeEntry(host="off.example.com", uri="/x")]
        )


def test_purging_everything_and_named_entries_at_once_is_refused(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )  # type: ignore[arg-type]
    _site(control, repository)

    with pytest.raises(ConflictError):
        build_cache_service(control).purge_cache(
            "alice",
            entries=[
                PurgeEntry(host="cdn.example.com", uri="/x", scheme=HttpScheme.HTTP)
            ],
            purge_all=True,
        )


def test_a_purge_with_nothing_to_do_is_refused(settings):
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )  # type: ignore[arg-type]
    with pytest.raises(ConflictError):
        build_cache_service(control).purge_cache("alice")


def test_purging_everything_needs_no_site_to_exist(settings):
    """--all is about the cache on disk, not about what is currently declared."""
    fake = FakeRunner([_purge_run()])
    control = ControlPlane(
        settings=settings, repository=Repository(settings.database_path), runner=fake
    )  # type: ignore[arg-type]

    result = build_cache_service(control).purge_cache("alice", purge_all=True)

    assert result.complete is True
    assert purges(fake)[0][1] is True


def test_a_partial_purge_is_reported_as_incomplete(settings):
    """Some edges dropped the object and some did not: clients see both."""
    repository = Repository(settings.database_path)
    partial = ansible_run(
        host_run("edge-a", changed=1), host_run("edge-b", ok=0, unreachable=1)
    )
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner([partial])
    )  # type: ignore[arg-type]
    _site(control, repository)

    result = build_cache_service(control).purge_cache(
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
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner([ansible_run(status=RunStatus.FAILED, return_code=1)]),
    )  # type: ignore[arg-type]
    _site(control, repository)

    with pytest.raises(ExecutionError, match="no edge reported"):
        build_cache_service(control).purge_cache(
            "alice",
            entries=[
                PurgeEntry(host="cdn.example.com", uri="/x", scheme=HttpScheme.HTTP)
            ],
        )


def test_a_purge_is_recorded_in_the_audit_trail(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner([_purge_run()])
    )  # type: ignore[arg-type]
    _site(control, repository)

    build_cache_service(control).purge_cache(
        "alice",
        entries=[
            PurgeEntry(host="cdn.example.com", uri="/app.js", scheme=HttpScheme.HTTP)
        ],
    )

    event = repository.audit_log.list_audit_events()[0]
    assert event.action == "cache.purged"
    assert event.details["complete"] is True
    assert event.details["entries"][0]["uri"] == "/app.js"
