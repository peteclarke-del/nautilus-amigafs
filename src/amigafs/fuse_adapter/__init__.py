"""FUSE 3 adapter boundary.

The adapter intentionally contains no image parsing. It will translate pyfuse3
operations into calls on the tested objects in :mod:`amigafs.core`.
"""

from __future__ import annotations

from importlib.util import find_spec


def runtime_available() -> bool:
    """Return whether the optional pyfuse3 runtime can be imported."""

    return find_spec("pyfuse3") is not None


__all__ = ["runtime_available"]
