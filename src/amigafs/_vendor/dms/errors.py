"""Error types the vendored DiskMasher decoder raises.

Amiga File Forge keeps these in its application package, which AmigaFS does not
import. They are reproduced here so the vendored decoder is unchanged.
"""

from __future__ import annotations


class DMSError(ValueError):
    """The bytes are not a usable DiskMasher archive."""


__all__ = ["DMSError"]
