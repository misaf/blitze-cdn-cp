"""The vocabulary core owns: values, with no machine anywhere near them.

A workflow, an Ansible run, a domain event, a hostname, the question every
capability contract answers. Each is a pydantic model or a regex — nothing here
opens a file, a socket, a subprocess or a database, and
`test_core_domain_and_ports_are_framework_and_io_independent` holds that by
refusing this package the imports that would. The sentence stood here for a
while before the test did, which is how the SDK came to publish this package
whole on a promise nothing checked.

Layer-first naming, deliberately, and only here. The capability tree is
organised by what a slice *is* rather than by what layer it belongs to, because
a site's rules, storage and routes change together. `core` has no slices to
make: it is the layer every slice stands on, so its own parts are named for the
role that decides who may import them.
"""
