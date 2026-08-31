"""Generated checks for domain invariants with broad input spaces."""

from hypothesis import given
from hypothesis import strategies as st

from blitzecdn.core.validation import hostname
from blitzecdn.features.deployments.snapshots import (
    decode_snapshot_zones,
    encode_snapshot,
)
from blitzecdn.features.dns.domain import DnsRecord, Domain

_LABEL = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=20
)
_HOSTNAME = st.lists(_LABEL, min_size=2, max_size=5).map(".".join)


@given(_HOSTNAME)
def test_hostname_normalization_is_idempotent(name: str) -> None:
    normalized = hostname(f"  {name.upper()}.  ")
    assert normalized == name
    assert hostname(normalized) == normalized


@given(label=_LABEL, address=st.ip_addresses(v=4))
def test_zone_snapshot_round_trip(label: str, address: object) -> None:
    domains = [Domain(name=f"{label}.example.com")]
    records = [
        DnsRecord(
            domain=domains[0].name,
            name=f"cdn-{label}",
            value=str(address),
            proxied=True,
        )
    ]

    restored_domains, restored_records = decode_snapshot_zones(
        encode_snapshot(domains, records)
    )

    assert restored_domains == domains
    assert restored_records == records


def test_legacy_unversioned_snapshot_remains_readable() -> None:
    snapshot = (
        '{"domains":[{"name":"example.com"}],"records":['
        '{"domain":"example.com","name":"cdn","type":"A",'
        '"value":"192.0.2.1","ttl":300,"proxied":true}]}'
    )

    domains, records = decode_snapshot_zones(snapshot)

    assert domains == [Domain(name="example.com")]
    assert records[0].fqdn == "cdn.example.com"
