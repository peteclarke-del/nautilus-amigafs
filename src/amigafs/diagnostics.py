"""Privacy-safe runtime diagnostics suitable for support attachments."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from amigafs.core.devices import trusted_helper
from amigafs.errors import AmigaFSError
from amigafs.fuse_adapter.availability import fuse_device_accessible
from amigafs.mounts import MountRecord, active_mounts
from amigafs.preferences import mount_location
from amigafs.privacy import safe_name

_SAFE_MOUNT_OPTIONS = frozenset({"ro", "rw", "nodev", "nosuid", "noexec", "relatime"})


def _image_identity(record: MountRecord) -> str | None:
    if record.image_device is None or record.image_inode is None:
        return None
    value = f"{record.image_device}:{record.image_inode}".encode("ascii")
    return hashlib.sha256(value).hexdigest()[:12]


def diagnostic_report() -> dict[str, Any]:
    """Collect operational metadata without image data or absolute paths."""

    try:
        version = importlib.metadata.version("nautilus-amigafs")
    except importlib.metadata.PackageNotFoundError:
        version = "development"
    mounts = active_mounts()
    try:
        location = mount_location()
        location_details = {"mode": location.mode, "source": location.source}
    except AmigaFSError:
        location_details = {"mode": "invalid", "source": "preferences-error"}
    return {
        "privacy": "No image contents or absolute paths are included.",
        "runtime": {
            "amigafs": version,
            "python": platform.python_version(),
            "implementation": sys.implementation.name,
            "platform": platform.system(),
            "architecture": platform.machine(),
        },
        "fuse": {
            "device_present": Path("/dev/fuse").exists(),
            "device_accessible": fuse_device_accessible(),
            "fusermount3_available": shutil.which("fusermount3") is not None,
        },
        "hardware": {
            "greaseweazle_tools": shutil.which("gw") is not None,
            "device_helper": trusted_helper() is not None,
            "polkit": shutil.which("pkexec") is not None,
        },
        "mount_location": location_details,
        "mounts": [
            {
                "mount_name": safe_name(record.mountpoint),
                "source_name": safe_name(record.source),
                "image_name": safe_name(record.image_path) if record.image_path else None,
                "image_identity": _image_identity(record),
                "image_kind": record.image_kind,
                "read_write": record.read_write,
                "registry_complete": record.image_path is not None,
                "options": ",".join(
                    option for option in record.options.split(",") if option in _SAFE_MOUNT_OPTIONS
                ),
                "pid": record.pid,
            }
            for record in mounts
        ],
    }


__all__ = ["diagnostic_report"]
