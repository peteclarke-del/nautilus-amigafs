"""Safe creation and publication of empty Amiga floppy and hard-disc images."""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from amigafs._vendor.amiganut.errors import AmiganutError
from amigafs._vendor.amiganut.filesystem import reader_for
from amigafs._vendor.amiganut.filesystem.amigados import format_volume, validate_name
from amigafs._vendor.amiganut.filesystem.blocks import FORMAT_LABELS, WRITABLE_FORMATS
from amigafs._vendor.amiganut.filesystem.pfs3_write import format_pfs3_volume
from amigafs._vendor.amiganut.filesystem.rdb import write_rigid_disk
from amigafs._vendor.amiganut.filesystem.sfs_write import format_sfs_volume
from amigafs.core.containers import DD_IMAGE_BYTES, HD_IMAGE_BYTES, SECTOR_BYTES
from amigafs.core.validation import validate_image_report
from amigafs.errors import AmigaFSError
from amigafs.i18n import _
from amigafs.operations import ProgressCallback, report_progress

_SAFE_STEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_CAPACITY = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMG]?)(?:I?B)?\s*$", re.IGNORECASE)
_UNITS = {"": 1, "K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024}

FLOPPY_DENSITIES = {"dd": DD_IMAGE_BYTES, "hd": HD_IMAGE_BYTES}
FLOPPY_FILESYSTEMS = tuple(WRITABLE_FORMATS)
HARD_DISC_FILESYSTEMS = (*WRITABLE_FORMATS, "PFS3", "SFS")
MINIMUM_HARD_DISC_BYTES = 2 * 1024 * 1024
MAXIMUM_HARD_DISC_BYTES = 64 * 1024 * 1024 * 1024
MAXIMUM_PARTITIONS = 16
HEADS = 16
SECTORS = 63


@dataclass(frozen=True, slots=True)
class CreatedImage:
    path: Path
    capacity_bytes: int
    title: str
    filesystem: str
    partitions: tuple[str, ...] = ()


def parse_capacity(text: str) -> int:
    """Parse ``40MB``, ``1.5G`` or a byte count into whole 512-byte sectors."""

    match = _CAPACITY.fullmatch(text)
    if match is None:
        raise AmigaFSError(
            _("The capacity must be a size such as 40MB or 1GB, not {text!r}.").format(text=text)
        )
    size = int(float(match[1]) * _UNITS[match[2].upper()])
    return size - size % SECTOR_BYTES


def normalise_filesystem(name: str, *, allowed: tuple[str, ...]) -> str:
    candidate = name.strip().upper().replace("_", "-") or "FFS"
    aliases = {"PFS": "PFS3", "FFS-INT": "FFS-INTL", "OFS-INT": "OFS-INTL"}
    candidate = aliases.get(candidate, candidate)
    if candidate not in allowed:
        raise AmigaFSError(
            _("Choose one of these filesystems: {choices}.").format(choices=", ".join(allowed))
        )
    return candidate


def _normalise_stem(name: str, default: str, suffixes: tuple[str, ...]) -> str:
    candidate = name.strip() or default
    path = Path(candidate)
    if path.name != candidate or candidate in {".", ".."}:
        raise AmigaFSError(_("The image name must be a filename, not a path."))
    if path.suffix.casefold() in suffixes:
        candidate = path.stem
    if not _SAFE_STEM.fullmatch(candidate):
        raise AmigaFSError(
            _(
                "The image name must contain 1–64 ASCII letters, digits, dots, dashes or "
                "underscores."
            )
        )
    return candidate


def _normalise_title(title: str) -> str:
    candidate = title.strip() or "Empty"
    try:
        candidate.encode("latin-1")
        return str(validate_name(candidate, 30))
    except (UnicodeEncodeError, AmiganutError) as exc:
        raise AmigaFSError(
            _("The volume name must contain 1–30 Latin-1 characters and no colon or slash.")
        ) from exc


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _destination(directory: str | Path, filename: str) -> tuple[Path, Path]:
    parent = Path(directory).expanduser().resolve()
    if not parent.is_dir():
        raise AmigaFSError(
            _("The image destination is not a directory: {path}").format(path=parent)
        )
    target = parent / filename
    try:
        collisions = [
            child.name for child in parent.iterdir() if child.name.casefold() == filename.casefold()
        ]
    except OSError as exc:
        raise AmigaFSError(
            _("Could not inspect the destination directory: {error}").format(error=exc)
        ) from exc
    if collisions:
        raise AmigaFSError(
            _("Image creation would overwrite existing file: {name}").format(name=collisions[0])
        )
    return parent, target


def _allocate(path: Path, size: int) -> None:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)
    try:
        os.ftruncate(descriptor, size)
    finally:
        os.close(descriptor)


def _validate_and_publish(
    temporary: Path, target: Path, parent: Path, progress: ProgressCallback | None
) -> None:
    report_progress(progress, 70, _("Validating the new image…"))
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    report = validate_image_report(temporary)
    problems = (*report.fatal_findings, *report.warning_findings)
    if problems:
        first = problems[0]
        raise AmigaFSError(
            _("Created image failed validation: {code}: {message}").format(
                code=first.code, message=first.message
            )
        )
    report_progress(progress, 90, _("Publishing the validated image…"))
    try:
        os.link(temporary, target)
        _sync_directory(parent)
    except OSError as exc:
        raise AmigaFSError(
            _("Could not publish the image without overwriting: {error}").format(error=exc)
        ) from exc


def create_floppy_image(
    directory: str | Path,
    *,
    name: str = "blank",
    title: str = "Empty",
    density: str = "dd",
    filesystem: str = "OFS",
    bootable: bool = False,
    progress: ProgressCallback | None = None,
) -> CreatedImage:
    """Create, fully validate and publish an empty ADF without overwriting files."""

    report_progress(progress, 0, _("Checking image settings…"))
    stem = _normalise_stem(name, "blank", (".adf",))
    volume_title = _normalise_title(title)
    size = FLOPPY_DENSITIES.get(density.strip().casefold() or "dd")
    if size is None:
        raise AmigaFSError(_("Choose a floppy density of dd (880 KiB) or hd (1760 KiB)."))
    label = normalise_filesystem(filesystem, allowed=FLOPPY_FILESYSTEMS)
    parent, target = _destination(directory, f"{stem}.adf")
    temporary = parent / f".{stem}.{uuid.uuid4().hex}.adf"
    try:
        report_progress(progress, 10, _("Formatting the empty volume…"))
        try:
            _allocate(temporary, size)
            reader = reader_for(temporary, writable=True)
            try:
                volume = format_volume(
                    reader,
                    label=volume_title,
                    dos_type=FORMAT_LABELS[label],
                    bootable=bootable,
                )
                volume.flush()
            finally:
                reader.close()
        except (AmiganutError, OSError) as exc:
            raise AmigaFSError(
                _("Could not create the floppy image: {error}").format(error=exc)
            ) from exc
        _validate_and_publish(temporary, target, parent, progress)
    finally:
        temporary.unlink(missing_ok=True)
    report_progress(progress, 100, _("Floppy image created and verified"))
    return CreatedImage(
        path=target, capacity_bytes=target.stat().st_size, title=volume_title, filesystem=label
    )


def create_hard_disc_image(
    directory: str | Path,
    *,
    name: str = "harddisk",
    title: str = "Empty",
    capacity: str = "40MB",
    filesystem: str = "FFS-INTL",
    partitions: int = 1,
    bootable: bool = True,
    progress: ProgressCallback | None = None,
) -> CreatedImage:
    """Create, validate and publish an RDB hard-disc image with formatted partitions."""

    report_progress(progress, 0, _("Checking image settings…"))
    stem = _normalise_stem(name, "harddisk", (".hdf",))
    volume_title = _normalise_title(title)
    label = normalise_filesystem(filesystem, allowed=HARD_DISC_FILESYSTEMS)
    size = parse_capacity(capacity.strip() or "40MB")
    if not MINIMUM_HARD_DISC_BYTES <= size <= MAXIMUM_HARD_DISC_BYTES:
        raise AmigaFSError(_("The capacity must be between 2 MiB and 64 GiB."))
    if not 1 <= partitions <= MAXIMUM_PARTITIONS:
        raise AmigaFSError(
            _("An image can be created with 1 to {maximum} partitions.").format(
                maximum=MAXIMUM_PARTITIONS
            )
        )
    cylinder = HEADS * SECTORS * SECTOR_BYTES
    size -= size % cylinder
    if size // cylinder < partitions + 1:
        raise AmigaFSError(_("The capacity is too small for that many partitions."))
    parent, target = _destination(directory, f"{stem}.hdf")
    temporary = parent / f".{stem}.{uuid.uuid4().hex}.hdf"
    dos_type = FORMAT_LABELS[label]
    share = (size - cylinder) // partitions
    names: list[str] = []
    try:
        try:
            _allocate(temporary, size)
            reader = reader_for(temporary, writable=True)
            try:
                report_progress(progress, 10, _("Writing the Rigid Disk Block…"))
                disk = write_rigid_disk(
                    reader,
                    [
                        {
                            "name": f"DH{index}",
                            "dosType": dos_type,
                            "sizeBytes": share,
                            "bootable": bootable and index == 0,
                            "bootPriority": 0 if index == 0 else -5,
                        }
                        for index in range(partitions)
                    ],
                    heads=HEADS,
                    sectors=SECTORS,
                    product="AMIGAFS HDF",
                )
                for position, partition in enumerate(disk.partitions):
                    report_progress(
                        progress,
                        15 + position * 50 // len(disk.partitions),
                        _("Formatting partition {name}…").format(name=partition.name),
                    )
                    volume_label = (
                        volume_title if position == 0 else f"{volume_title}{position}"[:30]
                    )
                    window = reader.window(partition.start_block, partition.total_blocks)
                    try:
                        if label == "PFS3":
                            format_pfs3_volume(window, label=volume_label)
                        elif label == "SFS":
                            format_sfs_volume(
                                window, label=volume_label, reserved=partition.reserved
                            )
                        else:
                            format_volume(
                                window,
                                label=volume_label,
                                dos_type=dos_type,
                                geometry=partition.geometry(),
                            ).flush()
                    finally:
                        window.close()
                    names.append(partition.name)
            finally:
                reader.close()
        except (AmiganutError, OSError) as exc:
            raise AmigaFSError(
                _("Could not create the hard-disc image: {error}").format(error=exc)
            ) from exc
        _validate_and_publish(temporary, target, parent, progress)
    finally:
        temporary.unlink(missing_ok=True)
    report_progress(progress, 100, _("Hard-disc image created and verified"))
    return CreatedImage(
        path=target,
        capacity_bytes=target.stat().st_size,
        title=volume_title,
        filesystem=label,
        partitions=tuple(names),
    )


__all__ = [
    "FLOPPY_DENSITIES",
    "FLOPPY_FILESYSTEMS",
    "HARD_DISC_FILESYSTEMS",
    "CreatedImage",
    "create_floppy_image",
    "create_hard_disc_image",
    "normalise_filesystem",
    "parse_capacity",
]
