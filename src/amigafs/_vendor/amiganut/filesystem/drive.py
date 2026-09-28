"""Building a hard drive: its partition table, its handlers and its volumes.

The modules beside this one each know one thing. ``rdb`` knows the partition
table, and ``amigados``, ``pfs3_write`` and ``sfs_write`` each know how to lay
down one filing system. A drive is all of them at once: a table naming several
partitions, a handler for every filing system Kickstart does not carry, and an
empty volume in each partition, formatted by whichever module its DOS type
belongs to. This module is where a request for a drive becomes those steps.

Nothing here writes more than the blocks that describe the drive. A new image
is a sparse file, so a drive of a hundred gigabytes takes a few megabytes of
the host's storage until files are put on it, and preparing a real card takes
seconds rather than the time it would take to write every block of it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..errors import ConfigurationError, DataError
from .amigados import format_volume
from .blocks import BLOCK_SIZE, DOS_TYPES, BlockReader
from .pfs3_blocks import MAXDISKSIZE4K, PFS3_DOS_TYPES
from .pfs3_write import format_pfs3_volume
from .rdb import (
    FileSystemHandler,
    Partition,
    RigidDisk,
    add_partition,
    as_dos_type,
    change_partition,
    geometry_for_drive,
    handler_version,
    partition_reader,
    read_rigid_disk,
    table_blocks_needed,
    write_rigid_disk,
)
from .sfs_write import format_sfs_volume

GIB = 1024 * 1024 * 1024

#: The names a request may use for a filing system, and the DOS type each
#: one puts in the partition table.
FILESYSTEM_DOS_TYPES = {
    "ofs": b"DOS\x00",
    "ffs": b"DOS\x01",
    "ofs-intl": b"DOS\x02",
    "ffs-intl": b"DOS\x03",
    "ofs-dc": b"DOS\x04",
    "ffs-dc": b"DOS\x05",
    "ofs-lnfs": b"DOS\x06",
    "ffs-lnfs": b"DOS\x07",
    "pfs3": b"PFS\x03",
    "pds3": b"PDS\x03",
    "sfs": b"SFS\x00",
}

#: AmigaDOS addresses a partition's blocks with a 32-bit number.
LARGEST_PARTITION_BLOCKS = 0xFFFFFFFF

#: The Smart File System in its first on-disk form, ``SFS\0``, places a
#: partition on the drive with byte offsets that stop at 127 GB.
LARGEST_SFS_BYTES = 127 * GIB

#: What is cleared at each end of a drive before it is given a new table.
#: A card from a camera or a computer carries its old partition table at the
#: start and, with GPT, a second copy at the end. Left there, the host finds
#: them again and offers to mount partitions that no longer exist.
DRIVE_WIPE_BYTES = 1024 * 1024


def dos_type_for(filesystem) -> bytes:
    """Turn a filing system's name, label or DOS type into a DOS type."""
    if isinstance(filesystem, (bytes, bytearray)):
        return as_dos_type(filesystem)
    text = str(filesystem or "").strip()
    key = text.lower().replace("_", "-").replace(" ", "-")
    if key in FILESYSTEM_DOS_TYPES:
        return FILESYSTEM_DOS_TYPES[key]
    for dos_type, label in DOS_TYPES.items():
        if label.lower() == key:
            return dos_type
    if len(text) == 4 and text.encode("latin-1", "replace") in DOS_TYPES:
        return text.encode("latin-1")
    raise ConfigurationError(f"{text or 'That'} is not a filing system this build can create.")


def family_of(dos_type: bytes) -> str:
    """Which formatter a DOS type belongs to: ``ffs``, ``pfs3`` or ``sfs``."""
    if dos_type in PFS3_DOS_TYPES:
        return "pfs3"
    if dos_type == b"SFS\x00":
        return "sfs"
    if dos_type[:3] == b"DOS" and dos_type in DOS_TYPES:
        return "ffs"
    raise ConfigurationError(
        f"This build cannot create a {DOS_TYPES.get(dos_type, 'volume of that kind')} volume."
    )


def needs_handler(dos_type: bytes) -> bool:
    """Whether a Kickstart 3.1 ROM lacks the handler for this DOS type.

    The ROM's FastFileSystem mounts ``DOS\\0`` to ``DOS\\5``. The long-name
    variants need the FastFileSystem of AmigaOS 3.1.4 or later, and the Smart
    and Professional filing systems are not part of any Kickstart.
    """
    return not (dos_type[:3] == b"DOS" and dos_type[3] <= 5)


def handler_satisfies(handler_type: bytes, partition_type: bytes) -> bool:
    """Whether a handler recorded for one DOS type serves a partition of another.

    The Professional File System ships as one binary that serves every one of
    its DOS types, so a drive carrying it for ``PFS\\3`` mounts a ``PDS\\3``
    partition once the handler is recorded for that type as well.
    """
    if handler_type == partition_type:
        return True
    return handler_type in PFS3_DOS_TYPES and partition_type in PFS3_DOS_TYPES


def default_buffers(dos_type: bytes) -> int:
    """How many cache buffers a new partition is given."""
    return 80 if family_of(dos_type) == "ffs" else 300


def check_partition_size(dos_type: bytes, size_bytes: int) -> None:
    """Refuse a partition larger than its filing system can describe."""
    family = family_of(dos_type)
    blocks = size_bytes // BLOCK_SIZE
    if blocks > LARGEST_PARTITION_BLOCKS:
        raise ConfigurationError("A partition can hold at most 2 TB.")
    if family == "pfs3" and blocks > MAXDISKSIZE4K:
        raise ConfigurationError(
            "The Professional File System stops at about 1.6 TB for one partition."
        )
    if family == "sfs" and size_bytes > LARGEST_SFS_BYTES:
        raise ConfigurationError(
            "The Smart File System stops at 127 GB for one partition. "
            "Use the Professional File System for a larger one."
        )


def make_handler(dos_type, binary: bytes, version: int | None = None) -> FileSystemHandler:
    """Wrap a handler's load file for embedding in a partition table."""
    if binary[:4] != b"\x00\x00\x03\xf3":
        raise DataError(
            "That file is not an Amiga load file, so it cannot be a filing-system handler."
        )
    return FileSystemHandler(
        dos_type=dos_type_for(dos_type),
        seglist=bytes(binary),
        version=handler_version(binary) if version is None else int(version),
    )


def handlers_in(path: Path | str) -> list[FileSystemHandler]:
    """Return the handlers a drive or drive image carries in its table."""
    with BlockReader(path) as reader:
        return list(read_rigid_disk(reader).handlers)


@dataclass
class FormattedPartition:
    """One partition of a drive that has just been prepared."""

    partition: Partition
    label: str

    def to_dict(self) -> dict:
        return {**self.partition.to_dict(), "label": self.label}


def allocate_image(path: Path | str, size_bytes: int) -> None:
    """Create an image file of a given size without writing its contents.

    The file is sparse: it reports its full size and occupies only what is
    later written to it. That is what makes an image of a 128 GB card a thing
    a desktop machine can hold.
    """
    path = Path(path)
    if size_bytes < 32 * BLOCK_SIZE:
        raise ConfigurationError("A volume needs at least 16 KiB.")
    with path.open("wb") as handle:
        handle.truncate(size_bytes - size_bytes % BLOCK_SIZE)


def data_extents(path: Path | str, length: int | None = None) -> list[tuple[int, int]]:
    """Return the runs of a file that hold data, as offset and length.

    A sparse image is mostly holes, which read as zeros and occupy nothing.
    Copying only the runs between them is what keeps a copy of a drive of a
    hundred gigabytes as quick and as small as the drive's contents. Where the
    host cannot say which parts of a file are holes, the whole file is one
    run, which is slower and no less correct.
    """
    size = os.path.getsize(path) if length is None else int(length)
    extents: list[tuple[int, int]] = []
    with open(path, "rb") as handle:
        descriptor = handle.fileno()
        position = 0
        try:
            while position < size:
                try:
                    start = os.lseek(descriptor, position, os.SEEK_DATA)
                except OSError as error:
                    if error.errno == 6:  # ENXIO: nothing but a hole from here on
                        break
                    raise
                if start >= size:
                    break
                end = min(os.lseek(descriptor, start, os.SEEK_HOLE), size)
                if end > start:
                    extents.append((start, end - start))
                position = max(end, start + 1)
        except (OSError, AttributeError):
            return [(0, size)]
    return extents


def copy_data(
    source: Path | str,
    target,
    *,
    source_offset: int = 0,
    target_offset: int = 0,
    length: int | None = None,
    progress=None,
    chunk: int = 4 * 1024 * 1024,
) -> int:
    """Copy a range of one image into an open file, skipping what is empty.

    Returns how many bytes were written. ``progress`` is told how far through
    the range the copy has reached, and may raise to stop it.
    """
    size = os.path.getsize(source) if length is None else int(length)
    end = source_offset + size
    written = 0
    with open(source, "rb") as reader:
        for start, run in data_extents(source, end):
            first = max(start, source_offset)
            last = min(start + run, end)
            position = first
            while position < last:
                reader.seek(position)
                data = reader.read(min(chunk, last - position))
                if not data:
                    break
                if data.count(0) != len(data):
                    target.seek(target_offset + position - source_offset)
                    target.write(data)
                    written += len(data)
                position += len(data)
                if progress is not None:
                    progress(position - source_offset, size)
    if progress is not None:
        progress(size, size)
    return written


def exact_shape(blocks: int) -> tuple[int, int]:
    """Choose heads and sectors whose cylinder divides a volume exactly.

    The FastFileSystem finds a volume's root block from the size its partition
    is declared with, so a partition wrapped around an existing volume has to
    be the volume's size to the block. Rounding it up to the next cylinder of
    a convenient size moves where the root block is looked for.
    """
    for heads, sectors in ((16, 128), (16, 63), (16, 32), (8, 32), (4, 32), (2, 32), (1, 32), (1, 16), (1, 8), (1, 2)):
        if blocks % (heads * sectors) == 0:
            return heads, sectors
    return 1, 1


#: What a volume with no partition table calls itself in its first block, and
#: the DOS type a partition holding it is given.
VOLUME_DOS_TYPES = {
    b"PFS\x01": b"PFS\x03",
    b"PFS\x02": b"PFS\x03",
    b"SFS\x00": b"SFS\x00",
}


def volume_dos_type(path: Path | str) -> bytes:
    """Read which filing system a bare volume holds from its first block."""
    with open(path, "rb") as handle:
        signature = handle.read(4)
    if signature in VOLUME_DOS_TYPES:
        return VOLUME_DOS_TYPES[signature]
    if signature[:3] == b"DOS" and signature in DOS_TYPES:
        return signature
    raise DataError("This volume does not begin with a filing system this build knows.")


def wrap_volume(
    source: Path | str,
    destination: Path | str,
    *,
    name: str = "DH0",
    handlers: list[FileSystemHandler] | None = None,
    bootable: bool = True,
    progress=None,
) -> RigidDisk:
    """Write a drive that holds one existing volume as its only partition.

    The volume's own bytes are copied across unchanged, into a partition of
    exactly the volume's size. What is added is the partition table in front
    of it, and the handler the volume needs.
    """
    source = Path(source)
    destination = Path(destination)
    volume_blocks = source.stat().st_size // BLOCK_SIZE
    if volume_blocks < 4:
        raise ConfigurationError("This image is too small to describe as a hard drive.")
    dos_type = volume_dos_type(source)
    entry = {
        "name": name,
        "dosType": dos_type,
        "bootable": bootable,
        "buffers": default_buffers(dos_type),
    }
    check_names([entry])
    embedded = handlers_for([entry], list(handlers or []))
    heads, sectors = exact_shape(volume_blocks)
    per_cylinder = heads * sectors
    reserved = max(1, -(-table_blocks_needed(8, embedded) // per_cylinder))
    entry["cylinders"] = volume_blocks // per_cylinder
    total_blocks = (reserved + entry["cylinders"]) * per_cylinder
    destination.unlink(missing_ok=True)
    try:
        with destination.open("wb") as handle:
            handle.truncate(total_blocks * BLOCK_SIZE)
        with BlockReader(destination, writable=True) as reader:
            disk = write_rigid_disk(
                reader,
                [entry],
                heads=heads,
                sectors=sectors,
                handlers=embedded,
                scale_to_fit=False,
            )
            partition = disk.partitions[0]
        with destination.open("r+b") as handle:
            copy_data(
                source,
                handle,
                target_offset=partition.start_block * BLOCK_SIZE,
                length=volume_blocks * BLOCK_SIZE,
                progress=progress,
            )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return disk


def extract_partition(
    source: Path | str,
    index: int,
    destination: Path | str,
    *,
    progress=None,
) -> Partition:
    """Copy one partition of a drive out as a volume with no partition table."""
    destination = Path(destination)
    with BlockReader(source) as reader:
        disk = read_rigid_disk(reader)
        if not disk.partitions:
            raise ConfigurationError("This drive declares no partitions to export.")
        partition = disk.partitions[index if 0 <= index < len(disk.partitions) else 0]
        available = reader.total_blocks - partition.start_block
    length = min(partition.total_blocks, max(0, available)) * BLOCK_SIZE
    destination.unlink(missing_ok=True)
    try:
        with destination.open("wb") as handle:
            handle.truncate(partition.total_blocks * BLOCK_SIZE)
            copy_data(
                source,
                handle,
                source_offset=partition.start_block * BLOCK_SIZE,
                length=length,
                progress=progress,
            )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return partition


def clear_drive_ends(reader: BlockReader) -> None:
    """Remove whatever described the drive before, at its start and its end."""
    length = reader.total_blocks * reader.block_size
    span = min(DRIVE_WIPE_BYTES, length)
    span -= span % reader.block_size
    if span <= 0:
        return
    reader.write_range(0, b"\0" * span)
    if length > 2 * span:
        reader.write_range(length - span, b"\0" * span)
    # A formatter opens the drive for itself, so what is written here has to
    # have reached the file first. Left in this handle's buffer, the zeros
    # would arrive after the volume and take its root block with them.
    reader.flush()


def format_window(
    window: BlockReader,
    dos_type: bytes,
    label: str,
    *,
    bootable: bool = False,
    partition: Partition | None = None,
) -> None:
    """Lay an empty volume of the right filing system across ``window``."""
    family = family_of(dos_type)
    check_partition_size(dos_type, window.total_blocks * window.block_size)
    if family == "pfs3":
        format_pfs3_volume(window, label=label)
        return
    if family == "sfs":
        sectors = partition.sectors_per_block if partition is not None else 1
        format_sfs_volume(
            window,
            label=label,
            block_size=window.block_size * max(1, sectors),
            reserved=partition.reserved if partition is not None else 2,
            prealloc=max(1, partition.preallocated) if partition is not None else 1,
        )
        return
    format_volume(
        window,
        label=label,
        dos_type=dos_type,
        bootable=bootable,
        geometry=partition.geometry() if partition is not None else None,
    )


def format_partition(
    reader: BlockReader, partition: Partition, label: str, dos_type: bytes | None = None
) -> None:
    """Format one partition of a drive, as its table says or as asked."""
    chosen = dos_type or partition.dos_type
    window = partition_reader(reader, partition)
    try:
        if window.total_blocks < partition.total_blocks:
            raise ConfigurationError(
                f"{partition.name} runs past the end of the drive, so it cannot be formatted."
            )
        # The start of the partition may hold the signature of whatever filing
        # system was there before. It is cleared first so that a volume which
        # names itself one thing is never found under a table saying another.
        window.write_range(0, b"\0" * min(64 * 1024, window.total_blocks * window.block_size))
        window.flush()
        format_window(window, chosen, label, bootable=partition.bootable, partition=partition)
    finally:
        window.close()


def partition_entry(spec: dict, index: int = 0) -> dict:
    """Turn a request for one partition into the fields the table writer takes."""
    dos_type = dos_type_for(spec.get("filesystem") or spec.get("dosType") or "ffs-intl")
    family_of(dos_type)
    size = spec.get("sizeBytes")
    size = int(size) if size not in (None, "", 0) else 0
    if size:
        check_partition_size(dos_type, size)
    entry = {
        "name": str(spec.get("name") or f"DH{index}").strip() or f"DH{index}",
        "dosType": dos_type,
        "sizeBytes": size,
        "bootable": bool(spec.get("bootable")),
        "bootPriority": int(spec.get("bootPriority") or 0),
        "automount": spec.get("automount", True) is not False,
        "buffers": int(spec.get("buffers") or default_buffers(dos_type)),
        "label": str(spec.get("label") or spec.get("name") or f"DH{index}"),
    }
    for key in ("maxTransfer", "mask", "lowCylinder", "cylinders"):
        if spec.get(key) is not None:
            entry[key] = int(spec[key])
    return entry


def check_names(entries: list[dict]) -> None:
    seen: set[str] = set()
    for entry in entries:
        name = entry["name"]
        if any(character in name for character in ":/ ") or len(name) > 30:
            raise ConfigurationError(
                f"{name!r} cannot be a device name. Use up to 30 letters and "
                "digits, with no space, colon or slash."
            )
        if name.lower() in seen:
            raise ConfigurationError(f"Two partitions are both called {name}.")
        seen.add(name.lower())


def missing_handlers(
    partitions: list, handlers: list[FileSystemHandler]
) -> list[bytes]:
    """Return the DOS types that would mount only with a handler not supplied."""
    wanted: list[bytes] = []
    for part in partitions:
        dos_type = part["dosType"] if isinstance(part, dict) else part.dos_type
        if not needs_handler(dos_type) or dos_type in wanted:
            continue
        if not any(handler_satisfies(handler.dos_type, dos_type) for handler in handlers):
            wanted.append(dos_type)
    return wanted


def handlers_for(
    partitions: list[dict], handlers: list[FileSystemHandler]
) -> list[FileSystemHandler]:
    """Record each supplied handler under every DOS type that asks for it.

    One handler file serves all the Professional File System's DOS types, and
    the table has to name it once for each type in use, because the machine
    looks a partition's handler up by the exact DOS type.
    """
    chosen: list[FileSystemHandler] = []
    for entry in partitions:
        dos_type = entry["dosType"]
        if not needs_handler(dos_type):
            continue
        if any(existing.dos_type == dos_type for existing in chosen):
            continue
        exact = [handler for handler in handlers if handler.dos_type == dos_type]
        related = [
            handler for handler in handlers if handler_satisfies(handler.dos_type, dos_type)
        ]
        source = (exact or related or [None])[0]
        if source is None:
            continue
        chosen.append(
            FileSystemHandler(
                dos_type=dos_type,
                seglist=source.seglist,
                version=source.version,
                patch_flags=source.patch_flags,
                stack_size=source.stack_size,
                priority=source.priority,
                global_vector=source.global_vector,
            )
        )
    return chosen


def initialise_drive(
    path: Path | str,
    partitions: list[dict],
    *,
    handlers: list[FileSystemHandler] | None = None,
    vendor: str = "AMIGA",
    product: str = "FILE FORGE HDF",
    revision: str = "1.1",
    progress=None,
) -> tuple[RigidDisk, list[FormattedPartition]]:
    """Give a drive a new partition table and an empty volume in each partition.

    ``path`` is an image file of the size the drive is to be, or the device
    node of a real one. Everything on it is given up. A partition with no size
    takes what the others leave.
    """
    if not partitions:
        raise ConfigurationError("A drive needs at least one partition.")
    entries = [partition_entry(spec, index) for index, spec in enumerate(partitions)]
    check_names(entries)
    embedded = handlers_for(entries, list(handlers or []))

    def report(message: str, done: int) -> None:
        if progress is not None:
            progress(message, done, len(entries) + 1)

    with BlockReader(path, writable=True) as reader:
        heads, sectors = geometry_for_drive(reader.total_blocks, reader.block_size)
        report("Writing the partition table", 0)
        clear_drive_ends(reader)
        disk = write_rigid_disk(
            reader,
            entries,
            heads=heads,
            sectors=sectors,
            vendor=vendor,
            product=product,
            revision=revision,
            handlers=embedded,
            scale_to_fit=False,
        )
        formatted: list[FormattedPartition] = []
        for index, (entry, partition) in enumerate(zip(entries, disk.partitions)):
            check_partition_size(partition.dos_type, partition.size_bytes)
            report(f"Formatting {partition.name}", index + 1)
            format_partition(reader, partition, entry["label"])
            formatted.append(FormattedPartition(partition, entry["label"]))
        reader.sync()
        report("The drive is ready", len(entries) + 1)
    return disk, formatted


def create_drive(
    path: Path | str,
    size_bytes: int,
    partitions: list[dict],
    **options,
) -> tuple[RigidDisk, list[FormattedPartition]]:
    """Create a new drive image of ``size_bytes`` and prepare it."""
    allocate_image(path, size_bytes)
    try:
        return initialise_drive(path, partitions, **options)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise


def create_volume(
    path: Path | str,
    size_bytes: int | None,
    filesystem,
    label: str,
    *,
    bootable: bool = False,
) -> bytes:
    """Create one volume with no partition table, across a whole file or drive.

    With a size, ``path`` is created as a sparse image of it. Without one it
    is a file or a device that is already there, and the volume fills it.
    """
    dos_type = dos_type_for(filesystem)
    if size_bytes is not None:
        check_partition_size(dos_type, size_bytes)
        allocate_image(path, size_bytes)
    try:
        with BlockReader(path, writable=True) as reader:
            if size_bytes is None:
                clear_drive_ends(reader)
            format_window(reader, dos_type, label, bootable=bootable)
            reader.sync()
    except Exception:
        if size_bytes is not None:
            Path(path).unlink(missing_ok=True)
        raise
    return dos_type


def add_formatted_partition(
    path: Path | str,
    spec: dict,
    *,
    handlers: list[FileSystemHandler] | None = None,
) -> FormattedPartition:
    """Add a partition in a drive's unused space and format it."""
    from .rdb import set_handler

    with BlockReader(path, writable=True) as reader:
        disk = read_rigid_disk(reader)
        entry = partition_entry(spec, len(disk.partitions))
        if not spec.get("name"):
            entry["name"] = ""
        else:
            check_names([entry])
        carried = disk.handlers + list(handlers or [])
        wanted = handlers_for([entry], carried)
        for handler in wanted:
            if not any(existing.dos_type == handler.dos_type for existing in disk.handlers):
                set_handler(reader, handler)
        partition = add_partition(reader, entry)
        try:
            check_partition_size(partition.dos_type, partition.size_bytes)
            label = str(spec.get("label") or partition.name)
            format_partition(reader, partition, label)
        except Exception:
            from .rdb import remove_partition

            remove_partition(reader, partition.index)
            raise
        reader.sync()
        return FormattedPartition(partition, label)


def reformat_partition(
    path: Path | str,
    index: int,
    label: str,
    filesystem=None,
    *,
    handlers: list[FileSystemHandler] | None = None,
) -> FormattedPartition:
    """Empty a partition, in the filing system it has or in another."""
    from .rdb import set_handler

    with BlockReader(path, writable=True) as reader:
        disk = read_rigid_disk(reader)
        if not 0 <= int(index) < len(disk.partitions):
            raise ConfigurationError(f"Partition {index} does not exist on this drive.")
        partition = disk.partitions[int(index)]
        dos_type = dos_type_for(filesystem) if filesystem else partition.dos_type
        family_of(dos_type)
        check_partition_size(dos_type, partition.size_bytes)
        if dos_type != partition.dos_type:
            entry = {"dosType": dos_type}
            for handler in handlers_for([entry], disk.handlers + list(handlers or [])):
                if not any(existing.dos_type == handler.dos_type for existing in disk.handlers):
                    set_handler(reader, handler)
            partition = change_partition(
                reader,
                int(index),
                {
                    "dosType": dos_type,
                    "buffers": default_buffers(dos_type),
                    "sectorsPerBlock": 1,
                    "reserved": 2,
                },
            )
        format_partition(reader, partition, label)
        reader.sync()
        return FormattedPartition(partition, label)


__all__ = [
    "DRIVE_WIPE_BYTES",
    "FILESYSTEM_DOS_TYPES",
    "FormattedPartition",
    "LARGEST_SFS_BYTES",
    "add_formatted_partition",
    "allocate_image",
    "check_partition_size",
    "clear_drive_ends",
    "create_drive",
    "create_volume",
    "copy_data",
    "data_extents",
    "default_buffers",
    "dos_type_for",
    "exact_shape",
    "extract_partition",
    "family_of",
    "format_partition",
    "format_window",
    "handler_satisfies",
    "handlers_for",
    "handlers_in",
    "initialise_drive",
    "make_handler",
    "missing_handlers",
    "needs_handler",
    "partition_entry",
    "reformat_partition",
    "volume_dos_type",
    "wrap_volume",
]
