"""Host access hardening for BlitzeCDN edges, as a distribution left out.

An edge is two machines in one: the containerised runtime that serves visitor
traffic, and the Ubuntu host underneath it that the control plane reaches over
SSH. Everything in this package is about the second one — public-key-only
authentication and a Fail2Ban jail in front of it — and nothing in it is about
the first.

That is what makes it detachable, and why it was extracted. A fleet whose host
access is managed elsewhere — a golden image, a configuration-management tool
that predates BlitzeCDN, a bastion that owns `sshd_config` — had no way to say
so while these roles were part of the control plane's own edge play: the play
named them, so every converge rewrote `/etc/ssh/sshd_config.d/50-blitzecdn.conf`
and `/etc/fail2ban/jail.d/blitzecdn-sshd.local` whether the operator wanted
BlitzeCDN to own those files or not. Detaching this distribution is now how a
fleet declines that, and it converges nothing else differently.

Attaching it is the default: `install.sh` and the container image pass
`--extra hardening`, so an ordinary installation hardens its edges exactly as
before the extraction.

Nothing here is imported by the control plane. Discovery is the
``blitzecdn.plugins`` entry point in this distribution's metadata and nothing
else.
"""

from blitzecdn.core.runtime.resources import distribution_version

#: This distribution's version, asked of the environment rather than
#: written down here: it is what ``PluginMetadata.version`` reports and
#: what ``blitzecdn plugins`` shows an operator, so the one number that
#: must not drift from ``pyproject.toml`` is not copied out of it.
__version__ = distribution_version(__name__)

__all__ = ["__version__"]
