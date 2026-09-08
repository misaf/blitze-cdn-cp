"""Package configuration boundaries."""

from __future__ import annotations


def test_core_carries_no_setting_named_for_an_optional_capability():
    """`Settings` is the platform's configuration, not a union of everyone's.

    A field named for a capability is loaded by every installation, including
    the ones that will never have the distribution — and a capability this
    repository has never heard of could not be configured at all. The generic
    answer is `capability_environment`, whose keys installed plugins must claim
    explicitly before core forwards them.
    """
    from blitzecdn.core.config import Settings

    # Implementation names, not capability tokens. A field called `backup_dir`
    # names a directory the platform creates and protects — `install.sh update`
    # writes there before it changes anything, with or without the `backup`
    # distribution — while a field called `maxmind_license_key` can only be
    # read by one wheel and is meaningless without it. The second kind is what
    # this refuses.
    forbidden = ("maxmind", "geolite", "under_attack", "brotli", "geoip", "njs")
    offenders = [
        name for name in Settings.model_fields for token in forbidden if token in name
    ]
    assert offenders == [], offenders
