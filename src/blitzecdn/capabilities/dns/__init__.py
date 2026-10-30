"""DNS zone, record, and service public contracts."""

from blitzecdn.capabilities.dns.adapters.documents import serving_document
from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, RecordPatch, RecordType
from blitzecdn.capabilities.dns.service import DnsService

__all__ = [
    "DnsRecord",
    "DnsService",
    "Domain",
    "RecordPatch",
    "RecordType",
    "serving_document",
]
