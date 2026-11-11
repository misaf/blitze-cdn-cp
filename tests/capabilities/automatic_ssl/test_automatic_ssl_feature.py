from types import SimpleNamespace

from blitzecdn.capabilities.automatic_ssl.feature import FEATURES
from blitzecdn.capabilities.automatic_ssl.settings import AutomaticSslConfig
from blitzecdn.capabilities.automatic_ssl.wiring import automatic_ssl_operations
from blitzecdn.composition import BUILTIN_REGISTRY


def test_metadata_names_the_capability() -> None:
    (feature,) = FEATURES

    assert feature.metadata.name == "automatic_ssl"
    assert [group.name for group in feature.cli_commands] == ["ssl"]


def test_the_scan_job_is_owned_by_automatic_ssl() -> None:
    calls: list[str] = []
    automatic_ssl = SimpleNamespace(
        reconcile=lambda operator: calls.append(f"scan {operator}")
    )

    operations = automatic_ssl_operations(
        automatic_ssl=automatic_ssl,
        config=AutomaticSslConfig(scan_interval_seconds=86_400),
    )
    (job,) = operations.scheduled_jobs

    assert (job.feature, job.name) == ("automatic_ssl", "automatic-ssl-scan")
    assert job.interval_seconds == 86_400
    assert job.jitter_seconds == 2880
    job.run("scheduler")
    assert calls == ["scan scheduler"]


def test_its_settings_belong_to_it_rather_than_to_tls() -> None:
    owners = {
        contribution.feature: {setting.name for setting in contribution.settings}
        for contribution in BUILTIN_REGISTRY.configuration_contributions()
    }

    assert "BLITZE_SSL_AUTOMATIC_SCAN_INTERVAL_SECONDS" in owners["automatic_ssl"]
    assert "BLITZE_SSL_AUTOMATIC_SCAN_INTERVAL_SECONDS" not in owners["certificates"]
