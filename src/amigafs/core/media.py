"""Opening a resolved source and finding the Amiga volumes it holds."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from amigafs._vendor.amiganut.filesystem import (
    AmigaDOSMount,
    PFS3Mount,
    SFSMount,
    geometry_from_geo,
)
from amigafs._vendor.amiganut.filesystem.amigados import AmigaDOSVolume
from amigafs._vendor.amiganut.filesystem.blocks import (
    DOS_TYPES,
    READ_ONLY_FORMATS,
    WRITABLE_FORMATS,
    Geometry,
)
from amigafs._vendor.amiganut.filesystem.pfs3 import PFS3Volume
from amigafs._vendor.amiganut.filesystem.pfs3_blocks import PFS3_DOS_TYPES
from amigafs._vendor.amiganut.filesystem.rdb import (
    Partition,
    RigidDisk,
    find_rdb_block,
    read_rigid_disk,
)
from amigafs._vendor.amiganut.filesystem.sfs import SFSVolume
from amigafs._vendor.amiganut.filesystem.sfs_blocks import SFS_ID
from amigafs._vendor.amiganut.kickfs.kickfs import KickstartMount
from amigafs.core.blockio import ImageStore
from amigafs.core.containers import Workspace, decode_container, source_signature
from amigafs.core.formats import ResolvedImage, resolve_image
from amigafs.errors import AmigaFSError, UnsupportedImageError
from amigafs.i18n import _
from amigafs.recovery import SessionCheckpoint, WorkspaceCheckpoint, pending_recovery

ProgressCallback = Callable[[int, str], None]
MAX_GEOMETRY_SIDECAR_BYTES = 4096
_UNSAFE_DIRECTORY = re.compile(r"[\x00-\x1f\x7f/]")


@dataclass(frozen=True, slots=True)
class VolumeInfo:
    """One filesystem found on a medium."""

    index: int
    directory: str
    device_name: str
    filesystem: str
    format: str
    dos_type: bytes
    offset: int
    length: int
    block_size: int
    writable: bool
    bootable: bool = False
    boot_priority: int = 0
    partition: Partition | None = None
    geometry: Geometry | None = None
    problem: str | None = None

    @property
    def mountable(self) -> bool:
        return self.problem is None


@dataclass(slots=True)
class OpenedMedia:
    """Every resource a mounted source holds, released together."""

    source: ResolvedImage
    store: ImageStore
    layout: str
    volumes: tuple[VolumeInfo, ...]
    writable: bool
    rigid_disk: RigidDisk | None = None
    workspace: Workspace | None = None
    identity_store: ImageStore | None = None
    checkpoint: SessionCheckpoint | WorkspaceCheckpoint | None = None
    notes: list[str] = field(default_factory=list)

    def close(self) -> None:
        self.store.close()
        if self.workspace is not None:
            self.workspace.close()
        if self.identity_store is not None:
            self.identity_store.close()


def _filesystem_for(dos_type: bytes) -> tuple[str, str, str | None]:
    """Return the driver name, the format label and any reason it cannot be mounted."""

    label = DOS_TYPES.get(dos_type)
    if dos_type == SFS_ID:
        return "sfs", label or "SFS", None
    if dos_type in PFS3_DOS_TYPES:
        return "pfs3", label or "PFS3", None
    if label is None:
        printable = dos_type[:3].decode("latin-1", "replace")
        return (
            "unknown",
            f"{printable}\\{dos_type[3]}" if len(dos_type) == 4 else "unknown",
            _("This partition's filesystem is not one AmigaFS can read."),
        )
    if label in READ_ONLY_FORMATS and label not in WRITABLE_FORMATS and label.startswith("SFS"):
        return "unknown", label, _("{format} volumes are not supported yet.").format(format=label)
    return ("ffs" if dos_type[3] & 1 else "ofs"), label, None


def _directory_name(name: str, index: int, taken: set[str]) -> str:
    cleaned = _UNSAFE_DIRECTORY.sub("_", name.strip()) or f"partition{index}"
    if cleaned in {".", ".."}:
        cleaned = f"partition{index}"
    candidate = cleaned
    suffix = 2
    while candidate.casefold() in taken:
        candidate = f"{cleaned}-{suffix}"
        suffix += 1
    taken.add(candidate.casefold())
    return candidate


def _geometry_sidecar(image: Path) -> Geometry | None:
    """Load a WinUAE ``.geo`` sidecar written beside an RDB-less hardfile."""

    for candidate in (image.with_suffix(image.suffix + ".geo"), image.with_suffix(".geo")):
        try:
            if not candidate.is_file() or candidate.stat().st_size > MAX_GEOMETRY_SIDECAR_BYTES:
                continue
            return geometry_from_geo(candidate.read_bytes())
        except Exception:
            continue
    return None


def detect_volumes(
    store: ImageStore, source: ResolvedImage, *, writable: bool
) -> tuple[str, tuple[VolumeInfo, ...], RigidDisk | None]:
    """Identify the layout of an opened medium from its content."""

    reader = store.reader(writable=False)
    if source.kind == "kickstart-rom":
        return (
            "kickstart",
            (
                VolumeInfo(
                    index=0,
                    directory="",
                    device_name="ROM",
                    filesystem="kickfs",
                    format="Kickstart",
                    dos_type=b"",
                    offset=0,
                    length=store.size,
                    block_size=512,
                    writable=False,
                ),
            ),
            None,
        )
    try:
        rdb_block = find_rdb_block(reader)
    except Exception:
        rdb_block = None
    if rdb_block is not None:
        try:
            disk = read_rigid_disk(reader)
        except Exception as exc:
            raise UnsupportedImageError(
                _("The Rigid Disk Block cannot be read: {error}").format(error=exc)
            ) from exc
        taken: set[str] = set()
        volumes: list[VolumeInfo] = []
        unit = disk.block_size or reader.block_size
        for partition in disk.partitions:
            filesystem, label, problem = _filesystem_for(partition.dos_type)
            offset = partition.start_block * unit
            length = partition.total_blocks * unit
            if problem is None and (length <= 0 or offset >= store.size):
                problem = _("The partition lies beyond the end of the medium.")
            elif problem is None and offset + length > store.size:
                problem = _("The partition extends beyond the end of the medium.")
            volumes.append(
                VolumeInfo(
                    index=partition.index,
                    directory=_directory_name(partition.name, partition.index, taken),
                    device_name=partition.name,
                    filesystem=filesystem,
                    format=label,
                    dos_type=partition.dos_type,
                    offset=offset,
                    length=length,
                    block_size=unit,
                    writable=writable and problem is None and filesystem != "unknown",
                    bootable=partition.bootable,
                    boot_priority=partition.boot_priority,
                    partition=partition,
                    geometry=partition.geometry(),
                    problem=problem,
                )
            )
        if not any(volume.mountable for volume in volumes):
            raise UnsupportedImageError(
                _("The hard disc has no partition with a filesystem AmigaFS can read.")
            )
        return "rdb", tuple(volumes), disk
    if reader.total_blocks == 0:
        raise UnsupportedImageError(_("The medium is empty."))
    signature = reader.read_block(0)[:4]
    filesystem, label, problem = _filesystem_for(signature)
    if filesystem == "unknown" or problem is not None:
        raise UnsupportedImageError(
            problem or _("No Amiga filesystem was found on {name}.").format(name=source.name)
        )
    geometry = None if source.is_device else _geometry_sidecar(source.primary_path)
    return (
        "single",
        (
            VolumeInfo(
                index=0,
                directory="",
                device_name="",
                filesystem=filesystem,
                format=label,
                dos_type=signature,
                offset=0,
                length=store.size,
                block_size=reader.block_size,
                writable=writable,
                geometry=geometry,
            ),
        ),
        None,
    )


def open_volume(store: ImageStore, volume: VolumeInfo, *, writable: bool) -> Any:
    """Open one detected volume with the driver its DOS type selects."""

    if not volume.mountable:
        raise UnsupportedImageError(volume.problem or _("This volume cannot be mounted."))
    reader = store.reader(
        offset=volume.offset,
        length=volume.length,
        block_size=volume.block_size,
        writable=writable and volume.writable,
    )
    try:
        if volume.filesystem == "kickfs":
            return KickstartMount(reader)
        if volume.filesystem == "sfs":
            return SFSMount(SFSVolume(reader))
        if volume.filesystem == "pfs3":
            return PFS3Mount(PFS3Volume(reader))
        return AmigaDOSMount(AmigaDOSVolume(reader, volume.geometry), volume.filesystem)
    except Exception as exc:
        raise UnsupportedImageError(
            _("The {format} volume cannot be opened: {error}").format(
                format=volume.format, error=exc
            )
        ) from exc


def _open_store(
    source: ResolvedImage,
    *,
    writable: bool,
    progress: ProgressCallback | None,
) -> tuple[
    ImageStore,
    Workspace | None,
    ImageStore | None,
    SessionCheckpoint | WorkspaceCheckpoint | None,
]:
    if source.container is None:
        if source.is_device:
            from amigafs.core.devices import open_device

            descriptor = open_device(source.primary_path, writable=writable)
            store = ImageStore.adopt(
                source.primary_path,
                descriptor,
                writable=writable,
                allow_device=True,
                display_name=source.name,
            )
        else:
            store = ImageStore.open(source.primary_path, writable=writable)
        return store, None, None, None

    identity = ImageStore.open(source.primary_path, writable=writable, check_links=writable)
    workspace: Workspace | None = None
    checkpoint: WorkspaceCheckpoint | None = None
    try:
        if writable:
            checkpoint = WorkspaceCheckpoint.create(
                source.primary_path, size=identity.size, detail=source.container
            )
        directory = checkpoint.directory if checkpoint is not None else None
        if source.container == "greaseweazle":
            from amigafs.greaseweazle import read_floppy_workspace

            if source.drive is None:
                raise AmigaFSError(_("No floppy drive was selected."))
            workspace = read_floppy_workspace(
                source.primary_path, source.drive, directory=directory, progress=progress
            )
        else:
            # The decode takes its own shared lock, so the session lock is
            # released for its duration and re-acquired before anything else
            # can be allowed to rely on the decoded copy.
            identity.close()
            workspace = decode_container(
                source.primary_path,
                kind=source.container,
                directory=directory,
                progress=progress,
            )
            identity = ImageStore.open(source.primary_path, writable=writable, check_links=writable)
            if workspace.source_signature != source_signature(
                identity.handle.fileno(), source.primary_path
            ):
                raise AmigaFSError(_("The image changed while AmigaFS was decoding it."))
            identity.expected_signature = identity.signature() if writable else None
        if writable and not workspace.writable_back:
            raise AmigaFSError(_("Read-write mounting is not supported for this image format."))
        store = ImageStore.open(workspace.raw_path, writable=writable, check_links=False)
        store.display_name = source.name
        return store, workspace, identity, checkpoint
    except BaseException:
        if workspace is not None:
            workspace.close()
        identity.close()
        if checkpoint is not None:
            with suppress(Exception):
                checkpoint.complete()
        raise


def open_media(
    selected: str | Path | ResolvedImage,
    *,
    writable: bool = False,
    progress: ProgressCallback | None = None,
) -> OpenedMedia:
    """Open, lock and classify one source."""

    source = selected if isinstance(selected, ResolvedImage) else resolve_image(selected)
    if writable and not source.capabilities.mount_read_write:
        raise AmigaFSError(_("Read-write mounting is not supported for this image format."))
    if writable and pending_recovery(source.primary_path) is not None:
        raise AmigaFSError(
            _(
                "An interrupted writable session needs recovery. Run "
                "'amigafs recover {path}' before mounting read-write."
            ).format(path=source.primary_path)
        )
    store, workspace, identity, checkpoint = _open_store(
        source, writable=writable, progress=progress
    )
    media = OpenedMedia(
        source=source,
        store=store,
        layout="",
        volumes=(),
        writable=writable,
        workspace=workspace,
        identity_store=identity,
        checkpoint=checkpoint,
    )
    try:
        media.layout, media.volumes, media.rigid_disk = detect_volumes(
            store, source, writable=writable
        )
    except BaseException:
        media.close()
        if checkpoint is not None:
            with suppress(Exception):
                checkpoint.complete()
        raise
    return media


__all__ = [
    "OpenedMedia",
    "VolumeInfo",
    "detect_volumes",
    "open_media",
    "open_volume",
]
