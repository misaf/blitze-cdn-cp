"""What the site editor decides.

`site.py` is the lifecycle of one virtual host: creating it, patching its
policy, enabling and deleting it. Every other service reads what this one
writes, which is why it is the only writer here.
"""

from blitzecdn.capabilities.sites.service.site import SiteService

__all__ = ["SiteService"]
