"""HTTP/3 across the fleet: one runtime switch and one listener owner.

A whole package for two variables looks like too much until you ask where else
they would go. They are derived from *every* site at once — whether any host
wants QUIC, and which single one carries `reuseport` — so they belong to no
site, and putting them in the renderer would make the renderer the thing every
future capability has to edit. Here they are a plugin like any other, and they
are the smallest complete example of one.
"""
