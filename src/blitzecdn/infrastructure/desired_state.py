"""Publish immutable snapshots in the format consumed by Ansible."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from blitzecdn.domain.sites import MANAGED_TLS_ROOT, CertificateMode
from blitzecdn.domain.snapshots import decode_snapshot
from blitzecdn.infrastructure.ansible_mapping import site_to_ansible
from blitzecdn.ports import CertificateStore, YamlWriter


class DesiredStateRenderer:
    def __init__(
        self,
        *,
        allow_empty_sites: bool,
        certificates: CertificateStore,
        write_yaml: YamlWriter,
    ) -> None:
        self.allow_empty_sites = allow_empty_sites
        self.certificates = certificates
        self.write_yaml = write_yaml

    def render(self, snapshot: str, path: Path) -> None:
        documents: list[dict[str, object]] = []
        for site in decode_snapshot(snapshot):
            document = site_to_ansible(site)
            if site.certificate_mode in {
                CertificateMode.UPLOADED,
                CertificateMode.REQUESTED,
            }:
                certificate, private_key = self.certificates.sources(site.name)
                document["certificate_source_path"] = str(certificate)
                document["certificate_key_source_path"] = str(private_key)
                destination = PurePosixPath(MANAGED_TLS_ROOT, site.name)
                document["certificate_path"] = str(destination / certificate.name)
                document["certificate_key_path"] = str(destination / private_key.name)
            documents.append(document)
        self.write_yaml(
            path,
            {
                "blitzecdn_nginx_allow_empty_sites": self.allow_empty_sites,
                "blitzecdn_nginx_sites": documents,
            },
        )
