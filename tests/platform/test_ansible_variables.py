"""Extra-vars files carry operator text that Ansible must never template."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from support.contracts import PROJECT_DIR

from blitzecdn.core.ansible.variables import (
    read_variables,
    run_variables,
    write_variables,
)
from blitzecdn.core.exceptions import ConfigurationError

INJECTION = "{{ 7*7 }}"
_APPEND_KEY = "{{ keys | default([]) + [entry.host ~ entry.uri] }}"
_REPORT = (
    "{{ {'keys': keys, 'page': sites[0].page, 'lookalikes': lookalikes} | to_json }}"
)


def _strings(node):
    """Every scalar node of a composed YAML document, keys included."""
    if isinstance(node, yaml.ScalarNode):
        yield node
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _strings(item)
    elif isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            yield from _strings(key)
            yield from _strings(value)


def test_variables_are_private_and_never_replace_a_symlink(tmp_path):
    target = tmp_path / "nested/run.yml"
    write_variables(target, {"secret": "masked"})
    assert os.stat(target).st_mode & 0o777 == 0o600

    real = tmp_path / "real"
    real.write_text("safe", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ConfigurationError, match="symlink"):
        write_variables(link, {"bad": True})
    assert real.read_text(encoding="utf-8") == "safe"


#: Strings a plain YAML scalar would read as a bool, number or null.
LOOKALIKES = {"ssl_mode": "off", "tls": "1.2", "flag": "yes", "none": "~", "empty": ""}


def test_every_string_is_tagged_unsafe_and_reads_back_as_data(tmp_path):
    values = {
        "blitzecdn_nginx_sites": [
            {
                "name": "example.com",
                "error_pages": {"maintenance_page": f"<h1>{INJECTION}</h1>\n<p>x</p>"},
                "aliases": ["a.example.com", INJECTION],
                "port": 443,
                "enabled": True,
                "missing": None,
            }
        ],
        "blitzecdn_cache_purge_all": False,
    }
    target = tmp_path / "vars.yml"
    write_variables(target, values)

    document = yaml.compose(target.read_text(encoding="utf-8"))
    strings = [
        node
        for node in _strings(document)
        if node.tag.endswith(":str") or "!" in node.tag
    ]
    assert strings
    assert {node.tag for node in strings} == {"!unsafe"}
    assert read_variables(target) == values


def test_a_string_ansible_would_retype_is_quoted_rather_than_tagged(tmp_path):
    """ansible-core re-resolves an ``!unsafe`` scalar, so ``!unsafe 'off'`` is False."""
    target = tmp_path / "vars.yml"
    write_variables(target, LOOKALIKES)

    document = yaml.compose(target.read_text(encoding="utf-8"))
    values = [value for _, value in document.value]
    assert all(node.style in ("'", '"') for node in values)
    assert all(node.tag != "!unsafe" for node in values)
    assert read_variables(target) == LOOKALIKES


def test_run_variables_are_written_unsafe_and_removed_after(tmp_path):
    with run_variables(tmp_path, "purge", {"uri": INJECTION}) as path:
        assert "!unsafe" in path.read_text(encoding="utf-8")
        assert read_variables(path) == {"uri": INJECTION}
    assert not path.exists()


def test_a_play_reads_operator_text_literally(tmp_path):
    """The shapes from #242: a purge key built from an item, and a looped site."""
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")

    marker = tmp_path / "marker"
    command = f"{{{{ lookup('pipe', 'touch {marker}') }}}}"
    variables = tmp_path / "vars.yml"
    write_variables(
        variables,
        {
            "entries": [{"host": "example.com", "uri": f"/{INJECTION}{command}"}],
            "sites": [
                {"name": "example.com", "page": f"<h1>{INJECTION}</h1>{command}"}
            ],
            "lookalikes": LOOKALIKES,
        },
    )
    output = tmp_path / "out.yml"
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "tasks": [
                        {
                            "assert": {"that": ["item.page | length > 0"]},
                            "loop": "{{ sites }}",
                        },
                        {
                            "set_fact": {"keys": _APPEND_KEY},
                            "loop": "{{ entries }}",
                            "loop_control": {"loop_var": "entry"},
                        },
                        {
                            "copy": {
                                "content": _REPORT,
                                "dest": str(output),
                                "mode": "0600",
                            }
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 - fixed ansible-playbook argv over tmp_path
        [
            ansible,
            "-i",
            "localhost,",
            "-c",
            "local",
            str(playbook),
            "-e",
            f"@{variables}",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert not marker.exists()
    rendered = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert rendered == {
        "keys": [f"example.com/{INJECTION}{command}"],
        "page": f"<h1>{INJECTION}</h1>{command}",
        "lookalikes": LOOKALIKES,
    }
