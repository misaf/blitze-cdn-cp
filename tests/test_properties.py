"""Generated checks for domain invariants with broad input spaces."""

from hypothesis import given
from hypothesis import strategies as st

from blitzecdn.domain.dns import DnsRecord, Domain
from blitzecdn.domain.snapshots import decode_snapshot_zones, encode_snapshot
from blitzecdn.domain.validation import hostname

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
