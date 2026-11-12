from typing import Annotated

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Path, Query, status

from blitzecdn.capabilities.sites import SiteService
from blitzecdn.capabilities.sites.api.models import (
    CdnSite,
    DnsExport,
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
from blitzecdn.capabilities.sites.domain import PatchPolicy
from blitzecdn.capabilities.sites.ports import SiteReader
from blitzecdn.capabilities.sites.service import RuleService
from blitzecdn.delivery.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.delivery.api.errors import (
    BODY_RESPONSES,
    CONFLICT_RESPONSES,
    NOT_FOUND_RESPONSES,
    OPERATOR_RESPONSES,
)
from blitzecdn.delivery.api.pagination import Page, paginate

router = APIRouter(
    route_class=DishkaSyncRoute,
    dependencies=[Depends(require_operator)],
    responses=OPERATOR_RESPONSES,
)


@router.get("/v3/domains", response_model=Page[Domain])
def list_domains(dns: FromDishka[SiteService]) -> Page[Domain]:
    return paginate(dns.list_domains(), Domain.from_domain)


@router.post(
    "/v3/domains",
    response_model=Domain,
    status_code=status.HTTP_201_CREATED,
    responses={**CONFLICT_RESPONSES, **BODY_RESPONSES},
)
def create_domain(
    domain: Domain, operator: OperatorDependency, dns: FromDishka[SiteService]
) -> Domain:
    return Domain.from_domain(dns.create_domain(domain.to_domain(), operator))


# Declared before ``/v3/domains/{domain}`` so ``export`` is not read as a zone.
@router.get("/v3/domains/export", response_model=DnsExport)
def export_domains(dns: FromDishka[SiteService]) -> DnsExport:
    """Desired DNS state for whatever is authoritative for these zones.

    An envelope, so ``publication`` makes plain that nothing here publishes it.
    """
    return DnsExport.from_domain(dns.dns_export())


@router.get(
    "/v3/domains/{domain}", response_model=Domain, responses=NOT_FOUND_RESPONSES
)
def get_domain(domain: str, dns: FromDishka[SiteService]) -> Domain:
    return Domain.from_domain(dns.get_domain(domain))


@router.patch(
    "/v3/domains/{domain}",
    response_model=Domain,
    responses={**NOT_FOUND_RESPONSES, **CONFLICT_RESPONSES, **BODY_RESPONSES},
)
def update_domain(
    domain: str,
    patch: DomainPatch,
    operator: OperatorDependency,
    dns: FromDishka[SiteService],
) -> Domain:
    return Domain.from_domain(
        dns.update_domain(domain, PatchPolicy.from_patch(patch.to_domain()), operator)
    )


@router.delete(
    "/v3/domains/{domain}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**NOT_FOUND_RESPONSES, **CONFLICT_RESPONSES},
)
def delete_domain(
    domain: str, operator: OperatorDependency, dns: FromDishka[SiteService]
) -> None:
    dns.delete_domain(domain, operator)


@router.get("/v3/sites", response_model=Page[CdnSite])
def list_sites(sites: FromDishka[SiteReader]) -> Page[CdnSite]:
    """The sites the zones and their rules resolve to; read-only.

    Its own collection, because a site belongs to a zone and to whichever rule
    claimed its hostnames. Change it through those.
    """
    return paginate(sites.list_sites(), CdnSite.from_domain)


@router.get("/v3/sites/{name}", response_model=CdnSite, responses=NOT_FOUND_RESPONSES)
def get_site(name: str, sites: FromDishka[SiteReader]) -> CdnSite:
    return CdnSite.from_domain(sites.get_site(name))


@router.get(
    "/v3/domains/{domain}/records",
    response_model=Page[DnsRecord],
    responses=NOT_FOUND_RESPONSES,
)
def list_records(domain: str, dns: FromDishka[SiteService]) -> Page[DnsRecord]:
    return paginate(dns.list_records(domain), DnsRecord.from_domain)


@router.post(
    "/v3/domains/{domain}/records",
    response_model=DnsRecord,
    status_code=status.HTTP_201_CREATED,
    responses={**NOT_FOUND_RESPONSES, **CONFLICT_RESPONSES, **BODY_RESPONSES},
)
def create_record(
    domain: str,
    record: RecordCreate,
    operator: OperatorDependency,
    dns: FromDishka[SiteService],
) -> DnsRecord:
    return DnsRecord.from_domain(dns.create_record(record.to_domain(domain), operator))


#: One record is addressed by its type as well as its name: an A and an AAAA
#: record share a name, and no type is assumed.
RECORD_PATH = "/v3/domains/{domain}/records/{type}/{name}"
RecordTypePath = Annotated[RecordType, Path(alias="type")]


@router.get(RECORD_PATH, response_model=DnsRecord, responses=NOT_FOUND_RESPONSES)
def get_record(
    domain: str, type_: RecordTypePath, name: str, dns: FromDishka[SiteService]
) -> DnsRecord:
    return DnsRecord.from_domain(dns.get_record(domain, name, type_.to_domain()))


@router.patch(
    RECORD_PATH,
    response_model=DnsRecord,
    responses={**NOT_FOUND_RESPONSES, **CONFLICT_RESPONSES, **BODY_RESPONSES},
)
def update_record(
    domain: str,
    type_: RecordTypePath,
    name: str,
    patch: RecordPatch,
    operator: OperatorDependency,
    dns: FromDishka[SiteService],
) -> DnsRecord:
    return DnsRecord.from_domain(
        dns.update_record(domain, name, type_.to_domain(), patch.to_domain(), operator)
    )


@router.delete(
    RECORD_PATH,
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**NOT_FOUND_RESPONSES, **CONFLICT_RESPONSES},
)
def delete_record(
    domain: str,
    type_: RecordTypePath,
    name: str,
    operator: OperatorDependency,
    dns: FromDishka[SiteService],
) -> None:
    dns.delete_record(domain, name, type_.to_domain(), operator)


@router.get(
    "/v3/domains/{domain}/export",
    response_model=DnsExport,
    responses=NOT_FOUND_RESPONSES,
)
def export_domain(domain: str, dns: FromDishka[SiteService]) -> DnsExport:
    """One zone's desired DNS state; see ``GET /v3/domains/export``."""
    return DnsExport.from_domain(dns.dns_export(domain))


@router.get(
    "/v3/domains/{domain}/rules",
    response_model=Page[Rule],
    responses=NOT_FOUND_RESPONSES,
)
def list_rules(domain: str, rules: FromDishka[RuleService]) -> Page[Rule]:
    return paginate(rules.list_rules(domain), Rule.from_domain)


@router.post(
    "/v3/domains/{domain}/rules",
    response_model=Rule,
    status_code=status.HTTP_201_CREATED,
    responses={**NOT_FOUND_RESPONSES, **CONFLICT_RESPONSES, **BODY_RESPONSES},
)
def create_rule(
    domain: str,
    rule: RuleCreate,
    operator: OperatorDependency,
    rules: FromDishka[RuleService],
) -> Rule:
    return Rule.from_domain(rules.create_rule(rule.to_domain(domain), operator))


@router.get(
    "/v3/domains/{domain}/rules/{name}",
    response_model=Rule,
    responses=NOT_FOUND_RESPONSES,
)
def get_rule(domain: str, name: str, rules: FromDishka[RuleService]) -> Rule:
    return Rule.from_domain(rules.get_rule(domain, name))


@router.patch(
    "/v3/domains/{domain}/rules/{name}",
    response_model=Rule,
    responses={**NOT_FOUND_RESPONSES, **CONFLICT_RESPONSES, **BODY_RESPONSES},
)
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
    "/v3/domains/{domain}/rules/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**NOT_FOUND_RESPONSES, **CONFLICT_RESPONSES},
)
def delete_rule(
    domain: str,
    name: str,
    operator: OperatorDependency,
    rules: FromDishka[RuleService],
) -> None:
    rules.delete_rule(domain, name, operator)


@router.get(
    "/v3/domains/{domain}/resolve",
    response_model=ResolvedPolicy,
    responses=NOT_FOUND_RESPONSES,
)
def resolve_hostname(
    domain: str,
    hostname: Annotated[
        str, Query(description="The hostname to resolve, e.g. api.example.com.")
    ],
    rules: FromDishka[RuleService],
    path: Annotated[
        str | None,
        Query(
            description=(
                "A request path, e.g. /static/app.css, to also apply the first "
                "path rule covering it. Omit to resolve the hostname alone."
            )
        ),
    ] = None,
) -> ResolvedPolicy:
    """How a hostname, or one path on it, is served, and which rules decided it."""
    return ResolvedPolicy.from_domain(rules.resolve(domain, hostname, path))
