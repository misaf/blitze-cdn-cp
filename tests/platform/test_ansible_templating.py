"""Compatibility checks against the installed ansible-core templating engine."""

import pytest
from ansible.errors import AnsibleUndefinedVariable

from blitzecdn.core.ansible.templating import AnsibleTemplates, load_variables


def test_yaml_precedence_trust_and_native_values(tmp_path):
    defaults = tmp_path / "defaults.yml"
    defaults.write_text('root: /old\npaths:\n  config: "{{ root }}/nginx"\n')
    group = tmp_path / "group.yml"
    group.write_text("root: /edge\nenabled: 'false'\n")

    templates = AnsibleTemplates(load_variables([defaults, group]))

    assert templates.value("paths") == {"config": "/edge/nginx"}
    assert templates.flag("enabled") is False
    assert templates.expression("{{ [1, 2] | length }}") == 2


def test_operator_values_remain_literal(tmp_path):
    source = tmp_path / "site.j2"
    source.write_text("{{ operator_value }}\n")
    value = "{{ lookup('env', 'BLITZE_TEMPLATE_SECRET') }}"
    templates = AnsibleTemplates({"operator_value": value})

    assert templates.value("operator_value") == value
    assert templates.render(source) == value + "\n"


def test_template_includes_filters_and_lookup(tmp_path, monkeypatch):
    directory = tmp_path / "templates"
    directory.mkdir()
    source = directory / "site.j2"
    source.write_text(
        "{% include 'fragment.j2' %}\n{{ lookup('env', 'BLITZE_TEMPLATE_TEST') }}\n"
    )
    (directory / "fragment.j2").write_text("{{ item | regex_replace('^edge-', '') }}\n")
    monkeypatch.setenv("BLITZE_TEMPLATE_TEST", "loaded")

    assert AnsibleTemplates({}).render(source, item="edge-one") == "oneloaded\n"


def test_template_metadata_and_local_values_do_not_leak(tmp_path):
    source = tmp_path / "metadata.j2"
    source.write_text(
        "{{ template_path }}|{{ template_fullpath }}|"
        "{{ template_destpath is none }}|{{ ansible_managed }}|{{ item }}\n"
    )
    templates = AnsibleTemplates({"ansible_managed": "managed", "item": "base"})

    assert templates.render(source, item="local") == (
        f"{source}|{source}|True|managed|local\n"
    )
    assert templates.value("item") == "base"


def test_default_managed_metadata_is_available(tmp_path):
    source = tmp_path / "metadata.j2"
    source.write_text("managed={{ ansible_managed is string }}\n")

    assert AnsibleTemplates({}).render(source) == "managed=True\n"


def test_whitespace_backslashes_and_empty_output(tmp_path):
    source = tmp_path / "whitespace.j2"
    source.write_text("{% if true %}\n  path \\d+\n{% endif %}\n")
    templates = AnsibleTemplates({})

    assert templates.render(source) == "  path \\d+\n"
    source.write_text("{% if false %}unused{% endif %}")
    assert templates.render(source) == ""


def test_missing_values_fail_instead_of_producing_partial_configuration(tmp_path):
    source = tmp_path / "invalid.j2"
    source.write_text("{{ missing_value }}")

    with pytest.raises(AnsibleUndefinedVariable):
        AnsibleTemplates({}).render(source)
