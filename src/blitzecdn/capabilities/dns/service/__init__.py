"""DNS services over canonical zones, records, and rules.

DnsService edits zones and records; RuleService edits policy overrides.
HostService derives virtual hosts and writes certificate and SSL results back
onto the zone or rule that produced them. Hosts are never stored separately.
"""

from blitzecdn.capabilities.dns.service.hosts import HostService
from blitzecdn.capabilities.dns.service.rules import RuleService
from blitzecdn.capabilities.dns.service.zones import DnsService

__all__ = ["DnsService", "HostService", "RuleService"]
