"""Publish immutable snapshots in the format consumed by Ansible."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from blitzecdn.core.ansible.mapping import site_to_ansible
from blitzecdn.features.certificates.ports import CertificateStore
from blitzecdn.features.deployments.ports import YamlWriter
from blitzecdn.features.deployments.snapshots import decode_snapshot
from blitzecdn.features.dns.site_domain import MANAGED_TLS_ROOT, CertificateMode


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
        http3_sites = sorted(
            str(document["name"])
            for document in documents
            if document.get("enabled", True) and document.get("http3_enabled", False)
        )
        self.write_yaml(
            path,
            {
                # The edge runtime contract's input, and the only place HTTP/3
                # is stated. It used to be written twice — once for the Nginx
                # listener and once for the firewall's UDP/443 rule — with the
                # edge play asserting the two copies agreed. One value cannot
                # disagree with itself, so the assertion went with the copy.
                "blitzecdn_edge_http3_enabled": bool(http3_sites),
                "blitzecdn_nginx_allow_empty_sites": self.allow_empty_sites,
                # Which site carries `reuseport` on the QUIC listener, which
                # nginx accepts on exactly one server block. Still the Nginx
                # role's: it is a rendering detail, not a runtime fact the
                # firewall or the stack has any use for.
                "blitzecdn_nginx_http3_listener_owner": (
                    http3_sites[0] if http3_sites else ""
                ),
                "blitzecdn_nginx_sites": documents,
            },
        )
