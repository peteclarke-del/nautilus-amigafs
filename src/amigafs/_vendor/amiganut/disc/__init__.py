"""Image addressing, mounting and the ``adisc`` command line."""

from .mount import ResolvedMount, resolve_mount, split_compound

__all__ = ["ResolvedMount", "resolve_mount", "split_compound"]
