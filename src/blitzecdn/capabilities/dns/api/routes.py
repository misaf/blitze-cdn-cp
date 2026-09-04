from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from blitzecdn.api.dependencies import (
    ControlPlaneDependency,
    OperatorDependency,
    require_operator,
)
from blitzecdn.capabilities.dns.api.models import (
    DnsRecord,
    Domain,
    RecordPatch,
    RecordType,
)
from blitzecdn.core.exceptions import ConflictError

router = APIRouter(dependencies=[Depends(require_operator)])


@router.get("/v1/domains", response_model=list[Domain])
def list_domains(control: ControlPlaneDependency) -> list[Domain]:
    return [Domain(name=item.name) for item in control.dns.list_domains()]


@router.post("/v1/domains", response_model=Domain, status_code=status.HTTP_201_CREATED)
def create_domain(
    domain: Domain, operator: OperatorDependency, control: ControlPlaneDependency
) -> Domain:
    return Domain(name=control.dns.create_domain(domain.to_domain(), operator).name)


@router.delete("/v1/domains/{domain}", status_code=status.HTTP_204_NO_CONTENT)
def delete_domain(
    domain: str, operator: OperatorDependency, control: ControlPlaneDependency
) -> None:
    control.dns.delete_domain(domain, operator)


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
def dns_export(control: ControlPlaneDependency) -> list[dict[str, object]]:
    return control.dns.dns_export()
