"""Amiganut error hierarchy.

``DataError`` covers media that is malformed, truncated or inconsistent.
``ConfigurationError`` covers a request the engine understands but cannot
carry out with the options it was given. Both are reported to the user by
Amiga File Forge; neither is a programming fault.
"""

from __future__ import annotations


class AmiganutError(Exception):
    """Base class for every engine error."""


class DataError(AmiganutError):
    """The media does not contain what the operation requires."""


class ConfigurationError(AmiganutError):
    """The requested operation is not possible with these options."""
