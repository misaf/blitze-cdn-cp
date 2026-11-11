"""Generated checks for domain invariants with broad input spaces."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from blitzecdn.capabilities.releases.domain import ReleaseInputs
from blitzecdn.capabilities.sites.domain import DnsRecord, Domain, derive_hosts
from blitzecdn.core.domain.validation import hostname
from blitzecdn.core.domain.values import IpAddress, IpNetwork, IpNetworkWidened

_LABEL = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=20
)
_HOSTNAME = st.lists(_LABEL, min_size=2, max_size=5).map(".".join)


@given(_HOSTNAME)
def test_hostname_normalization_is_idempotent(name: str) -> None:
    normalized = hostname(f"  {name.upper()}.  ")
    assert normalized == name
    assert hostname(normalized) == normalized


#: Everything `ipaddress` accepts after the `%`. Whitespace is left out because
#: `hostname` strips the ends first.
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

    `hostname` values render into nginx unquoted. Refusal is the property;
    `ipaddress` rejects a few zones itself, which fail as not-a-hostname.
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


#: Addresses a proxied record may name without its zone opting in: loopback,
#: link-local, private and the like are refused or need `allow_private_origin`.
#: Python counts multicast as global.
_PUBLIC_IPV4 = st.ip_addresses(v=4).filter(
    lambda address: address.is_global and not address.is_multicast
)


@given(label=_LABEL, address=_PUBLIC_IPV4)
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


_ADDRESS = TypeAdapter(IpAddress)
_NETWORK = TypeAdapter(IpNetwork)
_WIDENED = TypeAdapter(IpNetworkWidened)


@given(address=st.ip_addresses())
def test_shared_ip_types_store_the_canonical_spelling(address: object) -> None:
    """Every field typed with these compares equal for equal addresses."""
    canonical = str(address)
    spelled = f"  {canonical.upper()} "
    assert _ADDRESS.validate_python(spelled) == canonical
    prefix = address.max_prefixlen  # type: ignore[attr-defined]
    assert _NETWORK.validate_python(spelled) == f"{canonical}/{prefix}"
    assert _WIDENED.validate_python(spelled) == f"{canonical}/{prefix}"


@given(address=st.ip_addresses(v=6), zone=_ZONE)
def test_shared_ip_types_refuse_a_zone_index(address: object, zone: str) -> None:
    for adapter in (_ADDRESS, _NETWORK, _WIDENED):
        with pytest.raises(ValidationError):
            adapter.validate_python(f"{address}%{zone}")


@pytest.mark.parametrize(
    "value", ["fe80::1%eth0/64", "2001:db8::1%x/32", "::1%0/0", "fe80::1%eth0/128"]
)
def test_a_zone_index_is_refused_even_when_masking_would_drop_it(value: str) -> None:
    """`strict=False` masks host bits, and the zone with them (issue #316)."""
    with pytest.raises(ValidationError, match="zone indexes are not accepted"):
        _WIDENED.validate_python(value)
    with pytest.raises(ValidationError):
        _NETWORK.validate_python(value)


def test_only_the_widened_network_masks_host_bits() -> None:
    assert _WIDENED.validate_python("192.0.2.8/24") == "192.0.2.0/24"
    with pytest.raises(ValidationError, match="host bits set"):
        _NETWORK.validate_python("192.0.2.8/24")
