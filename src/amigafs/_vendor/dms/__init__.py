"""Vendored DiskMasher decoder from Amiga File Forge."""

from .dms import parse_dms, to_adf
from .errors import DMSError

__all__ = ["DMSError", "parse_dms", "to_adf"]
