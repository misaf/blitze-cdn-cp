"""Fleet origin probing for BlitzeCDN, as a distribution left out.

"Can the edges reach the origins they proxy to?" is an *operation*, not a piece
of desired state: it takes no deployment lock, changes nothing on any host, and
answers a question about the world rather than about configuration. Every other
operation of that kind is already a wheel — purging a cache, collecting cache
statistics, issuing a certificate — and this one was the last still wired into
the control plane's own Ansible adapter, with the play path carried as a core
setting.

Two things consume it, and both are here or declare a dependency on it:

* ``blitzecdn origin-check`` and ``POST /v{1,2}/origins/check``, this package's
  own command and routes;
* ``blitzecdn-certificates``' Automatic SSL/TLS scan, which probes each origin
  over its current transport and again under Full (strict) before recommending
  an upgrade. That distribution declares this one as a real dependency, so pip
  installs both and detaching this package cannot leave the scan importing
  something that is gone.

What stays in core is the single-origin row shape, ``OriginCheck``: the
controller's own advisory probe inside certificate preflight produces one, and
that probe is core's because it must answer in milliseconds during issuance and
cannot run a playbook.

Nothing here is imported by the control plane. Discovery is the
``blitzecdn.plugins`` entry point in this distribution's metadata and nothing
else.
"""

from blitzecdn.core.resources import distribution_version

#: This distribution's version, asked of the environment rather than
#: written down here: it is what ``PluginMetadata.version`` reports and
#: what ``blitzecdn plugins`` shows an operator, so the one number that
#: must not drift from ``pyproject.toml`` is not copied out of it.
__version__ = distribution_version(__name__)

__all__ = ["__version__"]
