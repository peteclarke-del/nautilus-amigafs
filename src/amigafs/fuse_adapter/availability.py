"""Side-effect-free checks for whether this process can use the FUSE device."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def fuse_device_accessible(device: str | Path = "/dev/fuse") -> bool:
    """Return true only when the device can actually be opened read-write."""

    try:
        descriptor = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return False
    os.close(descriptor)
    return True


def live_fuse_available() -> bool:
    return shutil.which("fusermount3") is not None and fuse_device_accessible()


__all__ = ["fuse_device_accessible", "live_fuse_available"]
