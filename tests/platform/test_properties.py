"""Generated checks for domain invariants with broad input spaces."""

from hypothesis import given
from hypothesis import strategies as st

from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, derive_hosts
from blitzecdn.capabilities.releases.domain import ReleaseInputs
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
def test_release_inputs_round_trip(label: str, address: object) -> None:
    domains = [Domain(name=f"{label}.example.com")]
    records = [
        DnsRecord(domain=domains[0].name, name=f"cdn-{label}", value=str(address))
    ]

    inputs = ReleaseInputs.of(domains, records, [])
    restored = ReleaseInputs.decode(inputs.encode())

    assert list(restored.domains) == domains
    assert list(restored.records) == records
    assert restored.rules == ()
    # Equal state must produce an equal digest, or a release is not
    # content-addressed and every comparison built on one is a coin toss.
    assert restored.digest == inputs.digest


@given(label=_LABEL, address=st.ip_addresses(v=4))
def test_release_inputs_derive_the_hosts_they_do_not_carry(
    label: str, address: object
) -> None:
    """What an edge is asked to serve is a function of what the inputs hold."""
    domains = [Domain(name=f"{label}.example.com")]
    records = [
        DnsRecord(domain=domains[0].name, name=f"cdn-{label}", value=str(address))
    ]

    restored = ReleaseInputs.decode(ReleaseInputs.of(domains, records, []).encode())
    (host,) = derive_hosts(
        list(restored.domains), list(restored.rules), list(restored.records)
    )

    assert host.server_names == (f"cdn-{label}.{domains[0].name}",)
    assert host.origin_host == str(address)
