from typing import Annotated, Any

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Query, status

from blitzecdn.capabilities.dns import DnsService
from blitzecdn.capabilities.dns.api.models import (
    CdnSite,
    DnsRecord,
    Domain,
    DomainPatch,
    RecordCreate,
    RecordPatch,
    RecordType,
    ResolvedPolicy,
    Rule,
    RuleCreate,
    RulePatch,
)
from blitzecdn.capabilities.dns.domain import PatchPolicy
from blitzecdn.capabilities.dns.ports import SiteReader
from blitzecdn.capabilities.dns.service import RuleService
from blitzecdn.entrypoints.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.entrypoints.api.pagination import Page, paginate

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


@router.get("/v3/domains", response_model=Page[Domain])
def list_domains(dns: FromDishka[DnsService]) -> Page[Domain]:
    return paginate(dns.list_domains(), Domain.from_domain)


@router.post("/v3/domains", response_model=Domain, status_code=status.HTTP_201_CREATED)
def create_domain(
    domain: Domain, operator: OperatorDependency, dns: FromDishka[DnsService]
) -> Domain:
    return Domain.from_domain(dns.create_domain(domain.to_domain(), operator))


@router.get("/v3/domains/{domain}", response_model=Domain)
def get_domain(domain: str, dns: FromDishka[DnsService]) -> Domain:
    return Domain.from_domain(dns.get_domain(domain))


@router.patch("/v3/domains/{domain}", response_model=Domain)
def update_domain(
    domain: str,
    patch: DomainPatch,
    operator: OperatorDependency,
    dns: FromDishka[DnsService],
) -> Domain:
    return Domain.from_domain(
        dns.update_domain(domain, PatchPolicy.from_patch(patch.to_domain()), operator)
    )


@router.delete("/v3/domains/{domain}", status_code=status.HTTP_204_NO_CONTENT)
def delete_domain(
    domain: str, operator: OperatorDependency, dns: FromDishka[DnsService]
) -> None:
    dns.delete_domain(domain, operator)


@router.get("/v3/hosts", response_model=Page[CdnSite])
def list_hosts(sites: FromDishka[SiteReader]) -> Page[CdnSite]:
    """The virtual hosts the zones and their rules resolve to; read-only.

    Its own collection, because a host belongs to a zone and to whichever rule
    claimed its hostnames. Change it through those.
    """
    return paginate(sites.list_sites(), CdnSite.from_domain)


@router.get("/v3/hosts/{name}", response_model=CdnSite)
def get_host(name: str, sites: FromDishka[SiteReader]) -> CdnSite:
    return CdnSite.from_domain(sites.get_site(name))


@router.get("/v3/domains/{domain}/records", response_model=Page[DnsRecord])
def list_records(domain: str, dns: FromDishka[DnsService]) -> Page[DnsRecord]:
    return paginate(dns.list_records(domain), DnsRecord.from_domain)


@router.post(
    "/v3/domains/{domain}/records",
    response_model=DnsRecord,
    status_code=status.HTTP_201_CREATED,
)
def create_record(
    domain: str,
    record: RecordCreate,
    operator: OperatorDependency,
    dns: FromDishka[DnsService],
) -> DnsRecord:
    return DnsRecord.from_domain(dns.create_record(record.to_domain(domain), operator))


@router.patch("/v3/domains/{domain}/records/{name}", response_model=DnsRecord)
def update_record(
    domain: str,
    name: str,
    patch: RecordPatch,
    operator: OperatorDependency,
    dns: FromDishka[DnsService],
    type_: Annotated[RecordType, Query(alias="type")] = RecordType.A,
) -> DnsRecord:
    return DnsRecord.from_domain(
        dns.update_record(domain, name, type_.to_domain(), patch.to_domain(), operator)
    )


@router.delete(
    "/v3/domains/{domain}/records/{name}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_record(
    domain: str,
    name: str,
    operator: OperatorDependency,
    dns: FromDishka[DnsService],
    type_: Annotated[RecordType, Query(alias="type")] = RecordType.A,
) -> None:
    dns.delete_record(domain, name, type_.to_domain(), operator)


@router.get("/v3/dns/export")
def dns_export(dns: FromDishka[DnsService]) -> dict[str, Any]:
    """Desired DNS state for whatever is authoritative for these zones.

    An envelope, so ``publication`` makes plain that nothing here publishes it.
    """
    return dns.dns_export()


@router.get("/v3/domains/{domain}/rules", response_model=Page[Rule])
def list_rules(domain: str, rules: FromDishka[RuleService]) -> Page[Rule]:
    return paginate(rules.list_rules(domain), Rule.from_domain)


@router.post(
    "/v3/domains/{domain}/rules",
    response_model=Rule,
    status_code=status.HTTP_201_CREATED,
)
def create_rule(
    domain: str,
    rule: RuleCreate,
    operator: OperatorDependency,
    rules: FromDishka[RuleService],
) -> Rule:
    return Rule.from_domain(rules.create_rule(rule.to_domain(domain), operator))


@router.get("/v3/domains/{domain}/rules/{name}", response_model=Rule)
def get_rule(domain: str, name: str, rules: FromDishka[RuleService]) -> Rule:
    return Rule.from_domain(rules.get_rule(domain, name))


@router.patch("/v3/domains/{domain}/rules/{name}", response_model=Rule)
def update_rule(
    domain: str,
    name: str,
    patch: RulePatch,
    operator: OperatorDependency,
    rules: FromDishka[RuleService],
) -> Rule:
    return Rule.from_domain(
        rules.update_rule(domain, name, patch.to_domain(), operator)
    )


@router.delete(
    "/v3/domains/{domain}/rules/{name}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_rule(
    domain: str,
    name: str,
    operator: OperatorDependency,
    rules: FromDishka[RuleService],
) -> None:
    rules.delete_rule(domain, name, operator)


@router.get("/v3/domains/{domain}/resolve", response_model=ResolvedPolicy)
def resolve_hostname(
    domain: str,
    hostname: Annotated[
        str, Query(description="The hostname to resolve, e.g. api.example.com.")
    ],
    rules: FromDishka[RuleService],
) -> ResolvedPolicy:
    """How a hostname is served, and which rule decided it, in one response."""
    return ResolvedPolicy.from_domain(rules.resolve(domain, hostname))
