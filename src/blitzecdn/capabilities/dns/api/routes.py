from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from blitzecdn.api.dependencies import (
    ControlPlaneDependency,
    OperatorDependency,
    require_operator,
)
from blitzecdn.capabilities.dns.api.models import (
    CdnSite,
    DnsRecord,
    Domain,
    DomainPatch,
    RecordPatch,
    RecordType,
    ResolvedPolicy,
    Rule,
    RuleCreate,
    RulePatch,
)
from blitzecdn.core.exceptions import ConflictError

router = APIRouter(dependencies=[Depends(require_operator)])


@router.get("/v1/domains", response_model=list[Domain])
def list_domains(control: ControlPlaneDependency) -> list[Domain]:
    return [Domain.from_domain(item) for item in control.dns.list_domains()]


@router.post("/v1/domains", response_model=Domain, status_code=status.HTTP_201_CREATED)
def create_domain(
    domain: Domain, operator: OperatorDependency, control: ControlPlaneDependency
) -> Domain:
    return Domain.from_domain(control.dns.create_domain(domain.to_domain(), operator))


@router.get("/v1/domains/{domain}", response_model=Domain)
def get_domain(domain: str, control: ControlPlaneDependency) -> Domain:
    return Domain.from_domain(control.dns.get_domain(domain))


@router.patch("/v1/domains/{domain}", response_model=Domain)
def update_domain(
    domain: str,
    patch: DomainPatch,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> Domain:
    return Domain.from_domain(
        control.dns.update_domain(domain, patch.to_domain(), operator)
    )


@router.delete("/v1/domains/{domain}", status_code=status.HTTP_204_NO_CONTENT)
def delete_domain(
    domain: str, operator: OperatorDependency, control: ControlPlaneDependency
) -> None:
    control.dns.delete_domain(domain, operator)


@router.get("/v1/hosts", response_model=list[CdnSite])
def list_hosts(control: ControlPlaneDependency) -> list[CdnSite]:
    """The virtual hosts the zones and their rules resolve to.

    Its own collection rather than a sub-resource of a zone, because a host is
    not inside one: it belongs to a zone *and* to whichever rule claimed its
    hostnames, and one of the two would have had to be the parent.

    Read-only, and there is no writing counterpart. Nothing authors these — to
    change one, change the zone, the rule or the records it came from.
    """
    return [CdnSite.from_domain(host) for host in control.sites.list_sites()]


@router.get("/v1/hosts/{name}", response_model=CdnSite)
def get_host(name: str, control: ControlPlaneDependency) -> CdnSite:
    return CdnSite.from_domain(control.sites.get_site(name))


@router.get("/v1/domains/{domain}/records", response_model=list[DnsRecord])
def list_records(domain: str, control: ControlPlaneDependency) -> list[DnsRecord]:
    return [DnsRecord.from_domain(item) for item in control.dns.list_records(domain)]


@router.post(
    "/v1/domains/{domain}/records",
    response_model=DnsRecord,
    status_code=status.HTTP_201_CREATED,
)
def create_record(
    domain: str,
    record: DnsRecord,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> DnsRecord:
    if record.domain != domain:
        raise ConflictError(
            f"record domain {record.domain!r} does not match the path "
            f"segment {domain!r}"
        )
    return DnsRecord.from_domain(
        control.dns.create_record(record.to_domain(), operator)
    )


@router.patch("/v1/domains/{domain}/records/{name}", response_model=DnsRecord)
def update_record(
    domain: str,
    name: str,
    patch: RecordPatch,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
    type_: Annotated[RecordType, Query(alias="type")] = RecordType.A,
) -> DnsRecord:
    return DnsRecord.from_domain(
        control.dns.update_record(
            domain, name, type_.to_domain(), patch.to_domain(), operator
        )
    )


@router.delete(
    "/v1/domains/{domain}/records/{name}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_record(
    domain: str,
    name: str,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
    type_: Annotated[RecordType, Query(alias="type")] = RecordType.A,
) -> None:
    control.dns.delete_record(domain, name, type_.to_domain(), operator)


@router.get("/v1/dns/export")
def dns_export(control: ControlPlaneDependency) -> dict[str, Any]:
    """Desired DNS state for whatever is authoritative for these zones.

    Unmodelled, and an envelope rather than a bare list. A client consuming
    this is being handed instructions for a system BlitzeCDN does not operate,
    so ``publication`` travels with them and says so — a caller cannot read the
    records without meeting the fact that nothing here publishes them.
    """
    return control.dns.dns_export()


@router.get("/v1/domains/{domain}/rules", response_model=list[Rule])
def list_rules(domain: str, control: ControlPlaneDependency) -> list[Rule]:
    return [Rule.from_domain(item) for item in control.rules.list_rules(domain)]


@router.post(
    "/v1/domains/{domain}/rules",
    response_model=Rule,
    status_code=status.HTTP_201_CREATED,
)
def create_rule(
    domain: str,
    rule: RuleCreate,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> Rule:
    return Rule.from_domain(control.rules.create_rule(rule.to_domain(domain), operator))


@router.get("/v1/domains/{domain}/rules/{name}", response_model=Rule)
def get_rule(domain: str, name: str, control: ControlPlaneDependency) -> Rule:
    return Rule.from_domain(control.rules.get_rule(domain, name))


@router.patch("/v1/domains/{domain}/rules/{name}", response_model=Rule)
def update_rule(
    domain: str,
    name: str,
    patch: RulePatch,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> Rule:
    return Rule.from_domain(
        control.rules.update_rule(domain, name, patch.to_domain(), operator)
    )


@router.delete(
    "/v1/domains/{domain}/rules/{name}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_rule(
    domain: str,
    name: str,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> None:
    control.rules.delete_rule(domain, name, operator)


@router.get("/v1/domains/{domain}/resolve", response_model=ResolvedPolicy)
def resolve_hostname(
    domain: str,
    hostname: Annotated[
        str, Query(description="The hostname to resolve, e.g. api.example.com.")
    ],
    control: ControlPlaneDependency,
) -> ResolvedPolicy:
    """How a hostname is served, and which rule decided it.

    The endpoint an operator reaches for when a hostname is not behaving like
    its zone: it answers with the merged policy *and* the rule's name, so the
    next question — which rule — does not need a second request and a manual
    match against the list.
    """
    return ResolvedPolicy.from_domain(control.rules.resolve(domain, hostname))
