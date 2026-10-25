"""Render a release's edge configuration on the controller, with Ansible's templar.

The templates stay the shipped role and capability templates, and they are
rendered by ansible-core's own templar, so filters, lookups and trust follow
exactly the semantics of the edge play. Variables are assembled in the play's
precedence order: role defaults in the order the edge play loads them, then the
edge group's shipped variables, then what `blitzecdn_config_release` binds when
it selects the candidate tree, then the extra-vars — the release's execution
document and the installed contributions.

This is the only renderer of edge configuration. A release is rendered once,
when it is prepared, and the rendering is stored with it; the edge stages those
bytes and renders nothing.

The release document and the contributions are rendered as *data*, never as
trusted templates, so an operator-authored value containing ``{{ ... }}`` is
written literally. Only the configuration tree is rendered here. Host state —
certificates, the ACME webroot, capability credentials, provider data — stays
with the edge play.

What is rendered where is the recipe in
:meth:`~blitzecdn.core.ansible.rendering.EdgeRenderer.render`, laid out as the
edge's candidate directory.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

from blitzecdn.core.ansible.contributions import EdgeContributions
from blitzecdn.core.domain.configuration import ConfigurationBundle
from blitzecdn.core.exceptions import ConfigurationError

__all__ = ["EdgeRenderer"]

#: The edge play's group, whose shipped variables apply to every edge.
_EDGE_GROUP = "blitzecdn_edges"

#: Roles whose defaults the edge play has loaded before `blitzecdn_nginx`
#: renders, around the capability roles that run in the edge slot.
_LEADING_ROLES = ("blitzecdn_edge", "blitzecdn_config_release")
_RENDERING_ROLE = "blitzecdn_nginx"

#: What `blitzecdn_config_release/tasks/select.yml` rebinds to the candidate.
_CANDIDATE_RUNTIME = (
    "{{ blitzecdn_edge_runtime | combine({'paths': {"
    "'nginx': blitzecdn_config_release_candidate ~ '/nginx', "
    "'modules_conf': blitzecdn_config_release_candidate ~ '/modules.conf', "
    "'modules': blitzecdn_config_release_candidate ~ '/njs'"
    "}}, recursive=True) }}"
)


class EdgeRenderer:
    """The edge's configuration tree for one release, from the shipped templates."""

    def __init__(self, *, ansible_dir: Path, contributions: EdgeContributions) -> None:
        self._ansible_dir = ansible_dir
        self._contributions = contributions

    def render(self, document: Mapping[str, Any]) -> ConfigurationBundle:
        """Render the candidate tree an edge would write for this document.

        ``document`` is a release's execution document: its desired-state
        artifact plus `blitzecdn_release_digest`.
        """
        context = _Context(self._variables(document))
        nginx = self._role(_RENDERING_ROLE) / "templates"
        files: dict[str, bytes] = {}
        links: dict[str, str] = {}

        def write(destination: str, content: str) -> None:
            files[context.relative(destination)] = content.encode("utf-8")

        paths = context.value("blitzecdn_edge_runtime")["paths"]
        write(
            context.value("blitzecdn_config_release_candidate") + "/nginx.conf",
            context.render(nginx / "nginx.conf.j2"),
        )
        write(
            paths["nginx"] + "/conf.d/blitzecdn-http.conf",
            context.render(nginx / "http.conf.j2"),
        )
        resources = context.value("blitzecdn_nginx_resources")
        for resource in resources.get("http", ()):
            write(
                paths["nginx"] + "/conf.d/blitzecdn-plugin-" + _plain(resource["name"]),
                context.render(Path(resource["template"])),
            )
        for resource in resources.get("module", ()):
            write(
                paths["modules"] + "/" + _plain(resource["name"]),
                context.render(Path(resource["template"])),
            )
        write(
            context.value("blitzecdn_nginx_status_file"),
            context.render(nginx / "status.conf.j2"),
        )
        write(paths["modules_conf"], context.render(nginx / "modules.conf.j2"))

        sites: list[Mapping[str, Any]] = list(context.value("blitzecdn_nginx_sites"))
        enabled = paths["nginx"] + "/sites-enabled/"
        for site in sites:
            available = paths["nginx"] + f"/sites-available/{site['name']}.conf"
            write(available, context.render(nginx / "site.conf.j2", item=site))
            if site.get("enabled", True):
                links[context.relative(enabled + f"{site['name']}.conf")] = available
        if context.flag("blitzecdn_nginx_default_server"):
            default = context.value("blitzecdn_nginx_default_site_file")
            write(default, context.render(nginx / "default.conf.j2"))
            links[context.relative(enabled + PurePosixPath(default).name)] = default
        names = sorted(str(site["name"]) for site in sites)
        write(
            context.value("blitzecdn_nginx_managed_sites_file"), "\n".join(names) + "\n"
        )
        return ConfigurationBundle(files=files, links=links)

    def _variables(self, document: Mapping[str, Any]) -> dict[str, Any]:
        from ansible.parsing.dataloader import DataLoader

        loader = DataLoader()
        variables: dict[str, Any] = {}
        roles = (*_LEADING_ROLES, *self._contributions.edge_roles, _RENDERING_ROLE)
        for name in roles:
            defaults = self._role(name) / "defaults" / "main.yml"
            if defaults.is_file():
                variables.update(_load(loader, defaults))
        group = self._ansible_dir / "inventory" / "group_vars" / _EDGE_GROUP
        for path in sorted(group.glob("*.yml")):
            variables.update(_load(loader, path))
        extra = {**dict(document), **self._contributions.variables()}
        if "blitzecdn_release_digest" not in extra:
            raise ConfigurationError("a release document names no release digest")
        variables.update(extra)
        context = _Context(variables)
        root = context.value("blitzecdn_config_release_root")
        variables["blitzecdn_config_release_candidate"] = (
            f"{root}/{extra['blitzecdn_release_digest']}"
        )
        variables["blitzecdn_config_release_enabled"] = True
        variables["blitzecdn_edge_runtime"] = _Context(variables).expression(
            _CANDIDATE_RUNTIME
        )
        # Facts lose to extra-vars, as they do in the play.
        variables.update(extra)
        return variables

    def _role(self, name: str) -> Path:
        for root in (self._ansible_dir / "roles", *self._contributions.roles_path):
            if (root / name).is_dir():
                return root / name
        raise ConfigurationError(f"role {name!r} is not on the role search path")


class _Context:
    """One set of variables, rendered with Ansible's templar."""

    def __init__(self, variables: dict[str, Any]) -> None:
        from ansible.parsing.dataloader import DataLoader
        from ansible.template import Templar

        _plugin_loader()
        self._variables = variables
        self._loader = DataLoader()
        self._templar = Templar(loader=self._loader, variables=variables)

    def expression(self, template: str) -> Any:
        from ansible.template import trust_as_template

        return self._templar.template(trust_as_template(template))

    def value(self, name: str) -> Any:
        return self.expression("{{ " + name + " }}")

    def flag(self, name: str) -> bool:
        return bool(self.expression("{{ " + name + " | bool }}"))

    def relative(self, destination: str) -> str:
        candidate = PurePosixPath(self.value("blitzecdn_config_release_candidate"))
        path = PurePosixPath(destination)
        if not path.is_relative_to(candidate):
            raise ConfigurationError(f"{destination} is outside the candidate tree")
        return str(path.relative_to(candidate))

    def render(self, source: Path, **local: Any) -> str:
        """Render one template file as the `template` action does."""
        from ansible._internal._templating import _template_vars
        from ansible.template import trust_as_template

        variables = {**self._variables, **local}
        variables.update(
            _template_vars.generate_ansible_template_vars(
                path=str(source),
                fullpath=str(source),
                dest_path=None,
                include_ansible_managed="ansible_managed" not in variables,
            )
        )
        searchpath = [str(source.parent), str(source.parent.parent)]
        templar = self._templar.copy_with_new_env(
            searchpath=[
                *(str(Path(path) / "templates") for path in searchpath),
                *searchpath,
            ],
            available_variables=variables,
        )
        rendered: str | None = templar.template(
            trust_as_template(source.read_text(encoding="utf-8")),
            escape_backslashes=False,
            overrides={
                "trim_blocks": True,
                "lstrip_blocks": False,
                "newline_sequence": "\n",
            },
        )
        # As the action does: a template that renders nothing writes an empty file.
        return rendered if rendered is not None else ""


@cache
def _plugin_loader() -> None:
    """Configure Ansible's plugin and collection loaders, once per process."""
    from ansible.plugins.loader import init_plugin_loader

    init_plugin_loader()


def _load(loader: Any, path: Path) -> Mapping[str, Any]:
    data = loader.load_from_file(str(path), trusted_as_template=True)
    return data or {}


def _plain(name: str) -> str:
    return name.removesuffix(".j2")
