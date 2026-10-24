"""Register the application's hardening implementation.

All code and resources ship with BlitzeCDN. Registration contributes delivery,
configuration or concrete edge resources; business operations remain services.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn.core.plugins import (
    AnsibleContribution,
    PluginMetadata,
    hookimpl,
)
from blitzecdn.providers.hardening import __version__, ansible


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="hardening",
        version=__version__,
        api_version=1,
        required=True,
        provides=frozenset({"hardening"}),
        summary="Public-key-only SSH and a Fail2Ban jail on every edge host.",
    )


@hookimpl
def blitzecdn_ansible_contributions() -> Sequence[AnsibleContribution]:
    # The roles, and the two slots that run them: the host slot in the edge
    # play converges, the teardown slot in the decommission play withdraws.
    # Core adds the directory to Ansible's search path, adds each name to the
    # slot that asked for it, and never learns what any of them contains.
    return (
        AnsibleContribution(
            plugin="hardening",
            roles_path=ansible.ROLES_PATH,
            host_roles=ansible.HOST_ROLES,
            teardown_roles=ansible.TEARDOWN_ROLES,
        ),
    )
