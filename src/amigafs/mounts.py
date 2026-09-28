"""Kernel-backed discovery and private lifecycle records for AmigaFS mounts."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from amigafs.core.formats import ResolvedImage, resolve_image
from amigafs.errors import AmigaFSError
from amigafs.i18n import _
from amigafs.safe_paths import atomic_write_private_text, ensure_private_directory

REGISTRY_VERSION = 1


@dataclass(frozen=True, slots=True)
class MountRecord:
    mountpoint: str
    source: str
    options: str
    image_path: str | None = None
    image_device: int | None = None
    image_inode: int | None = None
    image_kind: str | None = None
    image_name: str | None = None
    pid: int | None = None
    read_write: bool | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def runtime_root() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        root = Path(configured)
    else:
        root = Path("/run/user") / str(os.getuid())
        if not root.is_dir():
            root = Path(tempfile.gettempdir()) / f"amigafs-runtime-{os.getuid()}"
            root.mkdir(mode=0o700, exist_ok=True)
            details = root.stat(follow_symlinks=False)
            if details.st_uid != os.getuid() or details.st_mode & 0o077:
                raise AmigaFSError(
                    _("The fallback runtime directory is not private: {path}").format(path=root)
                )
    if not root.is_dir():
        raise AmigaFSError(_("The session runtime directory is unavailable."))
    return root / "amigafs"


def _registry_root(*, create: bool = False) -> Path:
    root = runtime_root() / "mounts"
    if create:
        ensure_private_directory(root, anchor=runtime_root().parent)
    return root


def _record_path(mountpoint: str | Path, *, create: bool = False) -> Path:
    target = str(Path(mountpoint).expanduser().resolve())
    digest = hashlib.sha256(target.encode("utf-8", "surrogateescape")).hexdigest()
    return _registry_root(create=create) / f"{digest}.json"


def mount_source_name(name: str) -> str:
    """Return the mount source name published to the kernel for one image."""

    cleaned = "".join(
        character if character.isalnum() or character in "._-+" else "_" for character in name
    ).strip("_")
    return cleaned[:64] or "amiga"


def _path_identity(path: Path) -> tuple[int, int]:
    """Identify a regular file by inode and a block device by its device number."""

    try:
        details = path.stat()
    except OSError as exc:
        raise AmigaFSError(
            _("Could not identify the Amiga image: {error}").format(error=exc)
        ) from exc
    if stat.S_ISBLK(details.st_mode):
        return details.st_rdev, 0
    return details.st_dev, details.st_ino


def _image_identity(image: ResolvedImage) -> tuple[int, int]:
    return _path_identity(image.primary_path)


def register_mount(
    image_path: str | Path | ResolvedImage, mountpoint: str | Path, *, read_write: bool
) -> MountRecord:
    """Atomically record the source identity held by a newly active mount."""

    image = image_path if isinstance(image_path, ResolvedImage) else resolve_image(image_path)
    image_device, image_inode = _image_identity(image)
    record = MountRecord(
        mountpoint=str(Path(mountpoint).expanduser().resolve()),
        source=mount_source_name(image.name),
        options="",
        image_path=str(image.primary_path),
        image_device=image_device,
        image_inode=image_inode,
        image_kind=image.kind,
        image_name=image.name,
        pid=os.getpid(),
        read_write=read_write,
    )
    path = _record_path(record.mountpoint, create=True)
    payload = {"version": REGISTRY_VERSION, **record.as_dict()}
    try:
        content = json.dumps(payload, sort_keys=True) + "\n"
        atomic_write_private_text(path, content, anchor=runtime_root().parent)
    except (OSError, MemoryError) as exc:
        raise AmigaFSError(
            _("Could not record the active AmigaFS mount: {error}").format(error=exc)
        ) from exc
    return record


def unregister_mount(mountpoint: str | Path) -> None:
    """Remove the lifecycle record for a mount that has fully shut down."""

    with suppress(OSError):
        _record_path(mountpoint).unlink(missing_ok=True)


def _decode_mount_field(value: str) -> str:
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n")


def parse_mountinfo(text: str) -> list[MountRecord]:
    mounts: list[MountRecord] = []
    for line in text.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            filesystem = fields[separator + 1]
            source = fields[separator + 2]
            mountpoint = fields[4]
            options = fields[5]
        except (IndexError, ValueError):
            continue
        if filesystem != "fuse.amigafs":
            continue
        mounts.append(
            MountRecord(
                mountpoint=_decode_mount_field(mountpoint),
                source=_decode_mount_field(source),
                options=options,
            )
        )
    return mounts


def _kernel_mounts() -> list[MountRecord]:
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError as exc:
        raise AmigaFSError(_("Cannot read mount status: {error}").format(error=exc)) from exc
    return parse_mountinfo(mountinfo)


def _registrations() -> dict[str, tuple[Path, MountRecord]]:
    try:
        root = _registry_root()
    except AmigaFSError:
        return {}
    if not root.is_dir():
        return {}
    registrations: dict[str, tuple[Path, MountRecord]] = {}
    for path in root.glob("*.json"):
        try:
            payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            if payload.pop("version") != REGISTRY_VERSION:
                continue
            record = MountRecord(**payload)
            registrations[record.mountpoint] = (path, record)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return registrations


def _process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def active_mounts() -> list[MountRecord]:
    """Return kernel-confirmed mounts enriched with exact private identity records."""

    kernel = _kernel_mounts()
    registrations = _registrations()
    enriched: list[MountRecord] = []
    for record in kernel:
        registration = registrations.get(record.mountpoint)
        if registration is None:
            enriched.append(record)
            continue
        private = registration[1]
        if not _process_alive(private.pid) or private.source != record.source:
            enriched.append(record)
            continue
        enriched.append(
            replace(
                record,
                image_path=private.image_path,
                image_device=private.image_device,
                image_inode=private.image_inode,
                image_kind=private.image_kind,
                image_name=private.image_name,
                pid=private.pid,
                read_write=private.read_write,
            )
        )
    for path, stale_record in registrations.values():
        if not _process_alive(stale_record.pid):
            with suppress(OSError):
                path.unlink(missing_ok=True)
    return enriched


def mount_at(mountpoint: str | Path) -> MountRecord | None:
    target = str(Path(mountpoint).expanduser().resolve())
    return next((record for record in active_mounts() if record.mountpoint == target), None)


def registered_mount_at(mountpoint: str | Path) -> MountRecord | None:
    """Return a live daemon record, including its post-detach validation window."""

    target = str(Path(mountpoint).expanduser().resolve())
    registration = _registrations().get(target)
    if registration is None:
        return None
    path, record = registration
    if _process_alive(record.pid):
        return record
    with suppress(OSError):
        path.unlink(missing_ok=True)
    return None


def mount_for_image(image_path: str | Path | ResolvedImage) -> MountRecord | None:
    """Resolve an image only when its canonical path and live identity still match."""

    image = image_path if isinstance(image_path, ResolvedImage) else resolve_image(image_path)
    image_device, image_inode = _image_identity(image)
    for record in active_mounts():
        if (
            record.image_path == str(image.primary_path)
            and record.image_device == image_device
            and record.image_inode == image_inode
        ):
            return record
    return None


def mount_for_image_path(image_path: str | Path) -> MountRecord | None:
    """Find a mount by canonical path and identity without parsing image content."""

    from amigafs.recovery import canonical_source

    selected = canonical_source(image_path)
    device, inode = _path_identity(selected)
    target = str(selected)
    for record in active_mounts():
        if (
            record.image_path == target
            and record.image_device == device
            and record.image_inode == inode
        ):
            return record
    return None


def is_mounted(mountpoint: str | Path) -> bool:
    return mount_at(mountpoint) is not None


def wait_for_mount_shutdown(mountpoint: str | Path, *, timeout: float = 35.0) -> bool:
    """Wait until a detached daemon finishes close-time flush and validation."""

    deadline = time.monotonic() + timeout
    while registered_mount_at(mountpoint) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    return registered_mount_at(mountpoint) is None


__all__ = [
    "MountRecord",
    "mount_source_name",
    "active_mounts",
    "is_mounted",
    "mount_at",
    "mount_for_image",
    "mount_for_image_path",
    "parse_mountinfo",
    "register_mount",
    "registered_mount_at",
    "runtime_root",
    "unregister_mount",
    "wait_for_mount_shutdown",
]
