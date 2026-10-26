"""Compatibility boundary for ansible-core's loading and template APIs."""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Any


def load_variables(paths: Iterable[Path]) -> dict[str, Any]:
    """Merge shipped YAML in precedence order, retaining Ansible's trust tags."""
    from ansible.parsing.dataloader import DataLoader

    loader = DataLoader()
    variables: dict[str, Any] = {}
    for path in paths:
        variables.update(
            loader.load_from_file(str(path), trusted_as_template=True) or {}
        )
    return variables


class AnsibleTemplates:
    """Render trusted templates while treating supplied values as data."""

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

    def render(self, source: Path, **local: Any) -> str:
        """Match the template action's metadata, search paths, and whitespace."""
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
        return rendered if rendered is not None else ""


@cache
def _plugin_loader() -> None:
    """Initialize plugin and collection loaders once per process."""
    from ansible.plugins.loader import init_plugin_loader

    init_plugin_loader()
