"""Two real SSH host keys, for tests about trusting an edge's key."""

from blitzecdn.capabilities.edges.domain import HostKey

# Generated with `ssh-keygen -t ed25519`; the fingerprints are what
# `ssh-keygen -lf` printed for them.
FIRST = HostKey(
    key_type="ssh-ed25519",
    key="AAAAC3NzaC1lZDI1NTE5AAAAIEC8PJnsgTvuJJJNbAQlakhNXO980JnOJ1vnlF+Sultl",
)
FIRST_FINGERPRINT = "SHA256:P9hMYoQ4Bx18RPTftWGE1HlL5WpTdcKBz2ZfUS7bVdA"
SECOND = HostKey(
    key_type="ssh-ed25519",
    key="AAAAC3NzaC1lZDI1NTE5AAAAIIdj+Rh+Jar4/LZuw4XuWaW4hTLtSmlBWpjRWN50ujEE",
)
SECOND_FINGERPRINT = "SHA256:T7hUyIZSPAZEA7/CD7+/p0GVoghyqZCchLoI4RAAFFs"
