"""Package configuration boundaries."""

from __future__ import annotations


def test_core_carries_no_setting_named_for_an_optional_capability():
    """`Settings` is the platform's configuration, not a union of everyone's.

    Capability configuration goes through `capability_environment`, whose keys
    installed features must claim before core forwards them.
    """
    from blitzecdn.core.config import Settings

    # Implementation names, not capability tokens: `backup_dir` is a directory the
    # platform itself creates and protects, while `maxmind_license_key` means
    # nothing outside one capability.
    forbidden = ("maxmind", "geolite", "under_attack", "brotli", "geoip", "njs")
    offenders = [
        name for name in Settings.model_fields for token in forbidden if token in name
    ]
    assert offenders == [], offenders
