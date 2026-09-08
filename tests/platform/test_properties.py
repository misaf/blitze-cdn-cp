"""Generated checks for domain invariants with broad input spaces."""

from hypothesis import given
from hypothesis import strategies as st

from blitzecdn.capabilities.deployments.domain.snapshots import (
    decode_snapshot,
    decode_snapshot_state,
    encode_snapshot,
)
from blitzecdn.capabilities.dns.domain import DnsRecord, Domain
from blitzecdn.core.domain.validation import hostname

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
    domains = [Domain(name=f"{label}.example.com", origin_host=str(address))]
    records = [DnsRecord(domain=domains[0].name, name=f"cdn-{label}")]

    restored_domains, restored_records, restored_rules = decode_snapshot_state(
        encode_snapshot(domains, records, [])
    )

    assert restored_domains == domains
    assert restored_records == records
    assert restored_rules == []


@given(label=_LABEL, address=st.ip_addresses(v=4))
def test_a_snapshot_derives_the_hosts_it_does_not_carry(
    label: str, address: object
) -> None:
    """What an edge is asked to serve is a function of what the snapshot holds."""
    domains = [Domain(name=f"{label}.example.com", origin_host=str(address))]
    records = [DnsRecord(domain=domains[0].name, name=f"cdn-{label}")]

    (host,) = decode_snapshot(encode_snapshot(domains, records, []))

    assert host.server_names == (f"cdn-{label}.{domains[0].name}",)
    assert host.origin_host == str(address)
