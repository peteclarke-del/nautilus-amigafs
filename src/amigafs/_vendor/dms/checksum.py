"""The one checksum helper the vendored DiskMasher decoder uses."""

from __future__ import annotations

import hashlib


def sha256_bytes(data: bytes) -> str:
    """Return the SHA-256 digest for an in-memory payload."""

    return hashlib.sha256(data).hexdigest()


__all__ = ["sha256_bytes"]
