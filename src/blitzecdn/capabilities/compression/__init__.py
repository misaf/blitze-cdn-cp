"""Response compression: the capability, its policy, and its strategies.

gzip and Brotli are *strategies* of this one capability, not capabilities of
their own — they answer the same question (may the edge compress this response,
and with what), share one on/off switch, one minimum length and one MIME type
list, and have no lifecycle either could have without the other. So they are
values of :class:`CompressionMode` here rather than two capability packages, and
``tests/architecture/test_layering.py`` refuses a top-level ``gzip`` or
``brotli`` package by name.
"""

from blitzecdn.capabilities.compression.policy import CompressionMode, CompressionPolicy

__all__ = ["CompressionMode", "CompressionPolicy"]
