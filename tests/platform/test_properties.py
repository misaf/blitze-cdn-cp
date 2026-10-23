"""Generated checks for domain invariants with broad input spaces."""

import pytest
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


#: Everything `ipaddress` accepts after the `%`: any character but `%` itself.
#: Whitespace is left out only because `hostname` strips the ends first, which
#: would change the input rather than the answer.
_ZONE = st.text(
    alphabet=st.characters(
        min_codepoint=0x21, max_codepoint=0x7E, exclude_characters="%"
    ),
    min_size=1,
    max_size=30,
)


@given(address=st.ip_addresses(v=6), zone=_ZONE)
def test_an_ipv6_zone_index_is_refused_whatever_it_contains(
    address: object, zone: str
) -> None:
    """The one suffix `ip_address` does not constrain, so it is never let in.

    `fe80::1%x;return 200 x` parsed as an address and came back unchanged, and
    every caller of `hostname` renders into nginx unquoted.

    Refusal is the property, not which message gives it: `ipaddress` turns a
    few zones away itself (`::%/`), and those fail as not-a-hostname instead.
    """
    assert hostname(str(address)) == str(address)
    with pytest.raises(ValueError):
        hostname(f"{address}%{zone}")


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
