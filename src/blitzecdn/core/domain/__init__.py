"""The vocabulary core owns: values, with no machine anywhere near them.

A workflow, an Ansible run, a domain event, a hostname, the question every
capability contract answers. Each is a pydantic model or a regex — nothing here
opens a file, a socket, a subprocess or a database, and the layering test holds
that by refusing this package the imports that would.

Layer-first naming, deliberately, and only here. The capability tree is
organised by what a slice *is* rather than by what layer it belongs to, because
a site's rules, storage and routes change together. `core` has no slices to
make: it is the layer every slice stands on, so its own parts are named for the
role that decides who may import them.
"""
