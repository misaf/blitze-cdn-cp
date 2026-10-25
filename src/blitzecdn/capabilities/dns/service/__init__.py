"""DNS services: zones and records, rule overrides, and derived hosts."""

from blitzecdn.capabilities.dns.service.hosts import HostService
from blitzecdn.capabilities.dns.service.rules import RuleService
from blitzecdn.capabilities.dns.service.zones import DnsService

__all__ = ["DnsService", "HostService", "RuleService"]
