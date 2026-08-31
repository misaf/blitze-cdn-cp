"""DNS zone, record, and service public contracts."""

from blitzecdn.features.dns.domain import DnsRecord, Domain, RecordPatch, RecordType
from blitzecdn.features.dns.service import DnsService

__all__ = [
    "DnsRecord",
    "DnsService",
    "Domain",
    "RecordPatch",
    "RecordType",
]
