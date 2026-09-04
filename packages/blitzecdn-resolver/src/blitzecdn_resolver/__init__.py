"""Host DNS resolution for BlitzeCDN edges, as a distribution left out.

An edge resolves names for three different reasons — apt fetches a package, the
runtime looks up an origin hostname, certificate issuance checks a CAA record —
and all three go through the host resolver. Where that resolver cannot be
trusted to answer public names truthfully, BlitzeCDN replaces it. Where it can,
BlitzeCDN has no business in `/etc/systemd/resolved.conf.d` at all.

That is the whole reason this is a distribution rather than a core role. The
role has always defaulted to off — many fleets run a legitimate internal
resolver, and silently replacing it on every converge would be a networking
change nobody requested — but "off" still meant a role in the control plane's
own edge play, named by a file the operator did not write, that every converge
evaluated on every host. Detaching this distribution is now how a fleet whose
DNS belongs to its network says so, and it converges nothing else differently.

Attaching it is the default: `install.sh` and the container image pass
`--extra resolver`, so a controller upgraded in place keeps managing resolution
on exactly the hosts it already was — the role is a no-op until
`blitzecdn_resolver_enabled` is set, exactly as before the extraction.

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
