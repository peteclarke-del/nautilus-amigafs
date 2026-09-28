"""Rigid Disk Block: the Amiga's on-drive partition table.

An Amiga hard drive describes itself. The first sixteen blocks hold an
``RDSK`` block, which chains to ``PART`` blocks for each partition, ``FSHD``
blocks for any filesystem the ROM does not already provide, and a bad-block
list. A ``.hdf`` that carries an RDB therefore holds several independently
mountable volumes in one file, each with its own name, DOS type, buffers and
boot priority -- which is exactly what the workbench presents as its partition
table.

An image with no RDB is a *hardfile*: one bare volume that the host has to be
told the geometry for. That case is handled by ``amiganut.filesystem.geometry``.

The field positions follow ``devices/hardblocks.h`` from the Amiga includes.
Drives written by releases of this engine up to 1.6 put the drive's vendor
strings and the number of the last RDB block in the wrong longs of the
``RDSK`` block. Nothing that mounts a partition reads those fields, so the
drives worked, but HDToolBox showed nonsense for them. Such a drive is still
read correctly here, and is put right the first time its table is rewritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..errors import ConfigurationError, DataError
from .blocks import (
    BLOCK_SIZE,
    DOS_TYPES,
    BlockReader,
    Geometry,
    long_at,
    put_long,
    put_signed_long,
    read_bstr,
    signed_long_at,
    write_bstr,
)

RDSK = b"RDSK"
PART = b"PART"
FSHD = b"FSHD"
LSEG = b"LSEG"
BADB = b"BADB"

#: The RDB must appear within the first sixteen blocks of the drive.
RDB_SEARCH_LIMIT = 16

END_OF_LIST = 0xFFFFFFFF

# PART flags
PARTF_BOOTABLE = 1
PARTF_NOMOUNT = 2

# DosEnvVec field offsets inside a PART block.
DE_BASE = 128
DE_TABLESIZE = DE_BASE + 0
DE_SIZEBLOCK = DE_BASE + 4
DE_SECORG = DE_BASE + 8
DE_SURFACES = DE_BASE + 12
DE_SECTORPERBLOCK = DE_BASE + 16
DE_BLKSPERTRACK = DE_BASE + 20
DE_RESERVEDBLKS = DE_BASE + 24
DE_PREFAC = DE_BASE + 28
DE_INTERLEAVE = DE_BASE + 32
DE_LOWCYL = DE_BASE + 36
DE_HIGHCYL = DE_BASE + 40
DE_NUMBUFFERS = DE_BASE + 44
DE_BUFMEMTYPE = DE_BASE + 48
DE_MAXTRANSFER = DE_BASE + 52
DE_MASK = DE_BASE + 56
DE_BOOTPRI = DE_BASE + 60
DE_DOSTYPE = DE_BASE + 64
DE_BAUD = DE_BASE + 68
DE_CONTROL = DE_BASE + 72
DE_BOOTBLOCKS = DE_BASE + 76

# RigidDiskBlock field offsets.
RDB_HOSTID = 12
RDB_BLOCKBYTES = 16
RDB_FLAGS = 20
RDB_BADBLOCKS = 24
RDB_PARTITIONS = 28
RDB_FILESYSTEMS = 32
RDB_DRIVEINIT = 36
RDB_CYLINDERS = 64
RDB_SECTORS = 68
RDB_HEADS = 72
RDB_INTERLEAVE = 76
RDB_PARK = 80
RDB_WRITEPRECOMP = 96
RDB_REDUCEDWRITE = 100
RDB_STEPRATE = 104
RDB_BLOCKSLO = 128
RDB_BLOCKSHI = 132
RDB_LOCYLINDER = 136
RDB_HICYLINDER = 140
RDB_CYLBLOCKS = 144
RDB_AUTOPARK = 148
RDB_HIGHRDSK = 152
RDB_VENDOR = 160
RDB_PRODUCT = 168
RDB_REVISION = 184

#: Where releases up to 1.6 wrote the same things.
LEGACY_HIGHRDSK = 92
LEGACY_PARK = 100
LEGACY_VENDOR = 128
LEGACY_PRODUCT = 136
LEGACY_REVISION = 152

# FileSysHeaderBlock field offsets.
FSHD_DOSTYPE = 32
FSHD_VERSION = 36
FSHD_PATCHFLAGS = 40
FSHD_STACKSIZE = 60
FSHD_PRIORITY = 64
FSHD_SEGLIST = 72
FSHD_GLOBALVEC = 76

#: How much of a handler one ``LSEG`` block carries after its five header longs.
LSEG_PAYLOAD = BLOCK_SIZE - 20

#: The host adapter's own SCSI address, as every Amiga controller records it.
HOST_ID = 7

#: The largest transfer that is safe on the IDE ports of an A600, A1200 and
#: A4000. A larger value is the classic cause of silent corruption on real
#: hardware, and costs nothing on an emulator.
SAFE_MAX_TRANSFER = 0x0001FE00
SAFE_MASK = 0x7FFFFFFE

#: A handler says which release it is in a ``$VER:`` string.
HANDLER_VERSION = re.compile(rb"\$VER:[^\x00]{0,60}?(\d+)\.(\d+)")


def rdb_checksum(block: bytes, longs: int) -> int:
    total = 0
    for index in range(longs):
        if index == 2:
            continue
        total = (total + long_at(block, index * 4)) & 0xFFFFFFFF
    return (-total) & 0xFFFFFFFF


def apply_rdb_checksum(block: bytearray, longs: int) -> bytearray:
    put_long(block, 8, rdb_checksum(bytes(block), longs))
    return block


def verify_rdb_checksum(block: bytes, longs: int) -> bool:
    total = 0
    for index in range(longs):
        total = (total + long_at(block, index * 4)) & 0xFFFFFFFF
    return total == 0


@dataclass
class Partition:
    """One ``PART`` entry, decoded into the fields the workbench shows."""

    index: int
    block: int
    name: str
    flags: int
    surfaces: int
    blocks_per_track: int
    sectors_per_block: int
    reserved: int
    low_cylinder: int
    high_cylinder: int
    buffers: int
    boot_priority: int
    dos_type: bytes
    mask: int
    max_transfer: int
    block_size: int = BLOCK_SIZE
    buffer_memory_type: int = 0
    preallocated: int = 0
    #: The ``PART`` block as it was read, kept so that rewriting the table
    #: carries across any field this build does not decode.
    raw: bytes = b""

    @property
    def bootable(self) -> bool:
        return bool(self.flags & PARTF_BOOTABLE)

    @property
    def automount(self) -> bool:
        return not self.flags & PARTF_NOMOUNT

    @property
    def blocks_per_cylinder(self) -> int:
        # The geometry is counted in sectors of ``block_size`` bytes, and a
        # cylinder is surfaces times sectors per track whatever the filing
        # system does with them. ``sectors_per_block`` only says how many of
        # those sectors the filing system groups into one of its own blocks,
        # as SFS does with 1024-byte blocks, so it takes no part in placing
        # the partition on the drive.
        return self.surfaces * self.blocks_per_track

    @property
    def start_block(self) -> int:
        return self.low_cylinder * self.blocks_per_cylinder

    @property
    def total_blocks(self) -> int:
        cylinders = self.high_cylinder - self.low_cylinder + 1
        return cylinders * self.blocks_per_cylinder

    @property
    def size_bytes(self) -> int:
        return self.total_blocks * self.block_size

    @property
    def format(self) -> str:
        return DOS_TYPES.get(self.dos_type, "unknown")

    def geometry(self) -> Geometry:
        return Geometry(
            surfaces=self.surfaces,
            blocks_per_track=self.blocks_per_track,
            reserved=self.reserved,
            block_size=self.block_size,
            low_cylinder=self.low_cylinder,
            high_cylinder=self.high_cylinder,
            sectors_per_block=self.sectors_per_block,
            boot_priority=self.boot_priority,
            dos_type=self.dos_type,
            mask=self.mask,
            max_transfer=self.max_transfer,
            buffers=self.buffers,
            label=self.name,
        )

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "device": self.name,
            "bootable": self.bootable,
            "automount": self.automount,
            "bootPriority": self.boot_priority,
            "dosType": self.dos_type.decode("latin-1"),
            "format": self.format,
            "surfaces": self.surfaces,
            "blocksPerTrack": self.blocks_per_track,
            "reserved": self.reserved,
            "lowCylinder": self.low_cylinder,
            "highCylinder": self.high_cylinder,
            "buffers": self.buffers,
            "startBlock": self.start_block,
            "totalBlocks": self.total_blocks,
            "sizeBytes": self.size_bytes,
            "maxTransfer": self.max_transfer,
            "mask": self.mask,
        }


def handler_version(binary: bytes) -> int:
    """Read a handler's version from its ``$VER:`` string, or return zero.

    The RDB records a version so that AmigaOS can tell one copy of a handler
    from another. Recording none would let any other copy on the system take
    precedence over the one the drive carries.
    """
    match = HANDLER_VERSION.search(binary)
    if match is None:
        return 0
    return (int(match.group(1)) << 16) | (int(match.group(2)) & 0xFFFF)


@dataclass
class FileSystemHandler:
    """A filing-system handler carried in the RDB as ``FSHD`` and ``LSEG`` blocks.

    Kickstart holds the FastFileSystem and nothing else. A partition in any
    other filing system mounts only if its handler travels with the drive,
    which is what these blocks are for: the machine loads the handler from
    them before it mounts the first partition.
    """

    dos_type: bytes
    seglist: bytes
    version: int = 0
    #: Which fields of the device node the handler entry replaces. ``0x180``
    #: names the segment list and the global vector, which is what HDToolBox
    #: writes for a handler that is an ordinary load file.
    patch_flags: int = 0x180
    stack_size: int = 0
    priority: int = 0
    global_vector: int = -1
    block: int = 0
    #: The blocks the handler occupied when it was read.
    table_blocks: list[int] = field(default_factory=list)

    @property
    def format(self) -> str:
        return DOS_TYPES.get(self.dos_type, "unknown")

    @property
    def blocks_needed(self) -> int:
        return 1 + max(1, -(-len(self.seglist) // LSEG_PAYLOAD))

    def to_dict(self) -> dict:
        return {
            "block": self.block,
            "dosType": self.dos_type.decode("latin-1"),
            "format": self.format,
            "version": f"{self.version >> 16}.{self.version & 0xFFFF}",
            "sizeBytes": len(self.seglist),
        }


@dataclass
class RigidDisk:
    """The decoded ``RDSK`` block and every partition it chains to."""

    block: int
    block_size: int
    cylinders: int
    sectors: int
    heads: int
    high_rdb_block: int
    park_cylinder: int
    partitions: list[Partition] = field(default_factory=list)
    filesystems: list[dict] = field(default_factory=list)
    disk_vendor: str = ""
    disk_product: str = ""
    disk_revision: str = ""
    handlers: list[FileSystemHandler] = field(default_factory=list)
    flags: int = 0x17
    host_id: int = HOST_ID
    #: The first and last cylinders a partition may use, and the last block
    #: reserved for the RDB itself.
    low_cylinder: int = 0
    high_cylinder: int = 0
    rdb_blocks_high: int = 0
    bad_block_list: int = END_OF_LIST
    drive_init: int = END_OF_LIST
    #: True for a table laid out the way releases up to 1.6 wrote it.
    legacy_layout: bool = False
    #: Every block the table occupied when it was read.
    table_blocks: list[int] = field(default_factory=list)

    @property
    def blocks_per_cylinder(self) -> int:
        return self.heads * self.sectors

    @property
    def cylinder_bytes(self) -> int:
        return self.blocks_per_cylinder * self.block_size

    @property
    def first_usable_cylinder(self) -> int:
        """The first cylinder a partition may start on."""
        return max(1, self.low_cylinder)

    def free_ranges(self) -> list[tuple[int, int]]:
        """Return the runs of cylinders no partition uses, as inclusive pairs."""
        taken = sorted(
            (part.low_cylinder, part.high_cylinder) for part in self.partitions
        )
        ranges: list[tuple[int, int]] = []
        cursor = self.first_usable_cylinder
        for low, high in taken:
            if low > cursor:
                ranges.append((cursor, low - 1))
            cursor = max(cursor, high + 1)
        if cursor <= self.cylinders - 1:
            ranges.append((cursor, self.cylinders - 1))
        return ranges

    def to_dict(self) -> dict:
        return {
            "blockSize": self.block_size,
            "cylinders": self.cylinders,
            "heads": self.heads,
            "sectors": self.sectors,
            "cylinderBytes": self.cylinder_bytes,
            "highRdbBlock": self.high_rdb_block,
            "vendor": self.disk_vendor,
            "product": self.disk_product,
            "revision": self.disk_revision,
            "partitions": [partition.to_dict() for partition in self.partitions],
            "filesystems": list(self.filesystems),
            "freeRanges": [
                {
                    "lowCylinder": low,
                    "highCylinder": high,
                    "sizeBytes": (high - low + 1) * self.cylinder_bytes,
                }
                for low, high in self.free_ranges()
            ],
        }


def find_rdb_block(reader: BlockReader) -> int | None:
    """Return the block holding the ``RDSK`` signature, or None."""
    limit = min(RDB_SEARCH_LIMIT, reader.total_blocks)
    for block in range(limit):
        if reader.read_block(block)[:4] == RDSK:
            return block
    return None


def _text(raw: bytes, offset: int, length: int) -> str:
    return raw[offset : offset + length].decode("latin-1").strip("\0 ")


def _is_legacy_layout(raw: bytes) -> bool:
    """Whether an ``RDSK`` block was laid out by a release up to 1.6.

    Those releases wrote the vendor, product and revision text from offset
    128, where the first and last RDB block numbers and the cylinder limits
    belong, and left the place the text belongs empty. Block and cylinder
    numbers are never 28 printable characters in a row.
    """
    text = raw[LEGACY_VENDOR : LEGACY_REVISION + 4]
    return all(32 <= byte < 127 for byte in text) and not any(
        raw[RDB_VENDOR : RDB_REVISION + 4]
    )


def _read_handler(reader: BlockReader, block: int, entry: bytes) -> FileSystemHandler:
    """Decode one ``FSHD`` block and gather the handler its ``LSEG`` chain holds."""
    payload = bytearray()
    segment = signed_long_at(entry, FSHD_SEGLIST)
    seen: set[int] = set()
    blocks = [block]
    while segment not in (-1, 0) and segment not in seen:
        if not 0 < segment < reader.total_blocks:
            raise DataError("The RDB filesystem handler chain is damaged.")
        seen.add(segment)
        data = reader.read_block(segment)
        if data[:4] != LSEG:
            break
        blocks.append(segment)
        longs = min(max(long_at(data, 4), 5), reader.block_size // 4)
        payload += data[20 : longs * 4]
        segment = signed_long_at(data, 16)
    handler = FileSystemHandler(
        dos_type=entry[FSHD_DOSTYPE : FSHD_DOSTYPE + 4],
        seglist=bytes(payload),
        version=long_at(entry, FSHD_VERSION),
        patch_flags=long_at(entry, FSHD_PATCHFLAGS),
        stack_size=long_at(entry, FSHD_STACKSIZE),
        priority=signed_long_at(entry, FSHD_PRIORITY),
        global_vector=signed_long_at(entry, FSHD_GLOBALVEC),
        block=block,
        table_blocks=blocks,
    )
    return handler


def read_rigid_disk(reader: BlockReader) -> RigidDisk:
    """Decode the RDB, its partitions and its filesystem handlers."""
    block_number = find_rdb_block(reader)
    if block_number is None:
        raise DataError("This image does not contain a Rigid Disk Block.")
    raw = reader.read_block(block_number)
    size_longs = long_at(raw, 4)
    if not 16 <= size_longs <= reader.block_size // 4:
        raise DataError("The RDSK block declares an impossible size.")
    if not verify_rdb_checksum(raw, size_longs):
        raise DataError("The RDSK block checksum is wrong.")
    legacy = _is_legacy_layout(raw)
    disk = RigidDisk(
        block=block_number,
        block_size=long_at(raw, RDB_BLOCKBYTES) or BLOCK_SIZE,
        cylinders=long_at(raw, RDB_CYLINDERS),
        sectors=long_at(raw, RDB_SECTORS),
        heads=long_at(raw, RDB_HEADS),
        high_rdb_block=long_at(raw, LEGACY_HIGHRDSK if legacy else RDB_HIGHRDSK),
        park_cylinder=long_at(raw, LEGACY_PARK if legacy else RDB_PARK),
        disk_vendor=_text(raw, LEGACY_VENDOR if legacy else RDB_VENDOR, 8),
        disk_product=_text(raw, LEGACY_PRODUCT if legacy else RDB_PRODUCT, 16),
        disk_revision=_text(raw, LEGACY_REVISION if legacy else RDB_REVISION, 4),
        flags=long_at(raw, RDB_FLAGS),
        host_id=long_at(raw, RDB_HOSTID),
        low_cylinder=0 if legacy else long_at(raw, RDB_LOCYLINDER),
        high_cylinder=0 if legacy else long_at(raw, RDB_HICYLINDER),
        rdb_blocks_high=0 if legacy else long_at(raw, RDB_BLOCKSHI),
        bad_block_list=long_at(raw, RDB_BADBLOCKS),
        drive_init=long_at(raw, RDB_DRIVEINIT),
        legacy_layout=legacy,
    )
    disk.table_blocks.append(block_number)

    partition_block = long_at(raw, RDB_PARTITIONS)
    index = 0
    seen: set[int] = set()
    while partition_block not in (0, END_OF_LIST):
        if partition_block in seen or partition_block >= reader.total_blocks:
            raise DataError("The RDB partition chain is damaged.")
        seen.add(partition_block)
        entry = reader.read_block(partition_block)
        if entry[:4] != PART:
            raise DataError(f"Block {partition_block} should hold a PART entry.")
        part_longs = long_at(entry, 4)
        if not verify_rdb_checksum(entry, part_longs):
            raise DataError(f"The PART block at {partition_block} has a bad checksum.")
        disk.partitions.append(
            Partition(
                index=index,
                block=partition_block,
                name=read_bstr(entry, 36, 31),
                flags=long_at(entry, 20),
                surfaces=long_at(entry, DE_SURFACES),
                blocks_per_track=long_at(entry, DE_BLKSPERTRACK),
                sectors_per_block=long_at(entry, DE_SECTORPERBLOCK) or 1,
                reserved=long_at(entry, DE_RESERVEDBLKS) or 2,
                low_cylinder=long_at(entry, DE_LOWCYL),
                high_cylinder=long_at(entry, DE_HIGHCYL),
                buffers=long_at(entry, DE_NUMBUFFERS),
                boot_priority=signed_long_at(entry, DE_BOOTPRI),
                dos_type=entry[DE_DOSTYPE : DE_DOSTYPE + 4],
                mask=long_at(entry, DE_MASK),
                max_transfer=long_at(entry, DE_MAXTRANSFER),
                block_size=(long_at(entry, DE_SIZEBLOCK) or 128) * 4,
                buffer_memory_type=long_at(entry, DE_BUFMEMTYPE),
                preallocated=long_at(entry, DE_PREFAC),
                raw=bytes(entry),
            )
        )
        disk.table_blocks.append(partition_block)
        index += 1
        partition_block = long_at(entry, 16)

    filesystem_block = long_at(raw, RDB_FILESYSTEMS)
    seen.clear()
    while filesystem_block not in (0, END_OF_LIST):
        if filesystem_block in seen or filesystem_block >= reader.total_blocks:
            raise DataError("The RDB filesystem chain is damaged.")
        seen.add(filesystem_block)
        entry = reader.read_block(filesystem_block)
        if entry[:4] != FSHD:
            break
        handler = _read_handler(reader, filesystem_block, entry)
        disk.handlers.append(handler)
        disk.table_blocks.extend(handler.table_blocks)
        disk.filesystems.append(handler.to_dict())
        filesystem_block = long_at(entry, 16)

    # The drive's own record of where partitions may begin is used when it is
    # sensible. Where it is missing or disagrees with the partitions, they
    # show how much room the drive's author left for the table. This is
    # settled once, here, so that removing the first partition does not make
    # the room it occupied look like part of the table.
    lowest = min((part.low_cylinder for part in disk.partitions), default=0)
    if disk.low_cylinder <= 0 or (lowest and disk.low_cylinder > lowest):
        per_cylinder = max(1, disk.blocks_per_cylinder)
        disk.low_cylinder = (
            max(1, lowest) if disk.partitions
            else max(1, -(-(max(disk.table_blocks) + 1) // per_cylinder))
        )
    return disk


def partition_reader(reader: BlockReader, partition: Partition) -> BlockReader:
    """Open a nested reader covering exactly one partition."""
    return reader.window(partition.start_block, partition.total_blocks)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def as_dos_type(value, variant: int = 3) -> bytes:
    """Accept a DOS type as four bytes, as text or as a format label."""
    if isinstance(value, (bytes, bytearray)):
        if len(value) != 4:
            raise ConfigurationError("A DOS type is four bytes long.")
        return bytes(value)
    text = str(value or "")
    if not text:
        return b"DOS\x03"
    if len(text) >= 4:
        return text.encode("latin-1")[:4]
    return text.encode("latin-1")[:3].ljust(3, b" ") + bytes([int(variant)])


def geometry_for_drive(total_blocks: int, block_size: int = BLOCK_SIZE) -> tuple[int, int]:
    """Choose the heads and sectors a new drive of this size is described with.

    A memory card has no cylinders, so the numbers are a convention, and what
    matters about them is the size of the cylinder they make: partitions start
    and end on cylinder boundaries, so it is the unit a partition is sized in.
    A small drive keeps the 16 by 63 every emulator assumes. From a gigabyte
    up a cylinder is one mebibyte, which keeps a drive of hundreds of
    gigabytes to a number of cylinders every tool can hold, and starts every
    partition on a boundary that suits flash storage.
    """
    if total_blocks * block_size >= 1024 * 1024 * 1024:
        return 16, 128
    return 16, 63


def _partition_block(
    partition: Partition, next_block: int, block_size: int, host_id: int = HOST_ID
) -> bytes:
    """Serialise one partition, keeping what an existing block already says."""
    if partition.raw and len(partition.raw) == block_size:
        raw = bytearray(partition.raw)
    else:
        raw = bytearray(block_size)
        raw[0:4] = PART
        put_long(raw, 4, 64)
        put_long(raw, 12, host_id)
        put_long(raw, DE_TABLESIZE, 16)
        put_long(raw, DE_SECORG, 0)
        put_long(raw, DE_INTERLEAVE, 0)
        put_long(raw, DE_BOOTBLOCKS, 0)
    put_long(raw, 16, next_block)
    put_long(raw, 20, partition.flags)
    write_bstr(raw, 36, partition.name[:30], 31)
    put_long(raw, DE_SIZEBLOCK, partition.block_size // 4)
    put_long(raw, DE_SURFACES, partition.surfaces)
    put_long(raw, DE_SECTORPERBLOCK, partition.sectors_per_block)
    put_long(raw, DE_BLKSPERTRACK, partition.blocks_per_track)
    put_long(raw, DE_RESERVEDBLKS, partition.reserved)
    put_long(raw, DE_PREFAC, partition.preallocated)
    put_long(raw, DE_LOWCYL, partition.low_cylinder)
    put_long(raw, DE_HIGHCYL, partition.high_cylinder)
    put_long(raw, DE_NUMBUFFERS, partition.buffers)
    put_long(raw, DE_BUFMEMTYPE, partition.buffer_memory_type)
    put_long(raw, DE_MAXTRANSFER, partition.max_transfer)
    put_long(raw, DE_MASK, partition.mask)
    put_signed_long(raw, DE_BOOTPRI, partition.boot_priority)
    raw[DE_DOSTYPE : DE_DOSTYPE + 4] = partition.dos_type
    apply_rdb_checksum(raw, long_at(raw, 4))
    return bytes(raw)


def _handler_blocks(
    handler: FileSystemHandler, first: int, next_handler: int, block_size: int, host_id: int
) -> dict[int, bytes]:
    """Serialise a handler as its ``FSHD`` block and the ``LSEG`` chain after it."""
    payload = block_size - 20
    chunks = [
        handler.seglist[offset : offset + payload]
        for offset in range(0, len(handler.seglist), payload)
    ] or [b""]
    blocks: dict[int, bytes] = {}
    header = bytearray(block_size)
    header[0:4] = FSHD
    put_long(header, 4, 64)
    put_long(header, 12, host_id)
    put_long(header, 16, next_handler)
    header[FSHD_DOSTYPE : FSHD_DOSTYPE + 4] = handler.dos_type
    put_long(header, FSHD_VERSION, handler.version)
    put_long(header, FSHD_PATCHFLAGS, handler.patch_flags)
    put_long(header, FSHD_STACKSIZE, handler.stack_size)
    put_signed_long(header, FSHD_PRIORITY, handler.priority)
    # The segment list field holds the number of the first LSEG block, not a
    # count of them, and AmigaOS looks for the handler nowhere else.
    put_signed_long(header, FSHD_SEGLIST, first + 1)
    put_signed_long(header, FSHD_GLOBALVEC, handler.global_vector)
    apply_rdb_checksum(header, 64)
    blocks[first] = bytes(header)
    for index, chunk in enumerate(chunks):
        number = first + 1 + index
        segment = bytearray(block_size)
        segment[0:4] = LSEG
        put_long(segment, 4, block_size // 4)
        put_long(segment, 12, host_id)
        put_long(
            segment, 16, number + 1 if index + 1 < len(chunks) else END_OF_LIST
        )
        segment[20 : 20 + len(chunk)] = chunk
        apply_rdb_checksum(segment, block_size // 4)
        blocks[number] = bytes(segment)
    return blocks


def table_blocks_needed(partitions: int, handlers: list[FileSystemHandler]) -> int:
    """How many blocks a table of this shape occupies, the ``RDSK`` included."""
    return 1 + partitions + sum(handler.blocks_needed for handler in handlers)


def store_rigid_disk(reader: BlockReader, disk: RigidDisk) -> RigidDisk:
    """Write a partition table to the drive and read it back.

    The table is laid out from the ``RDSK`` block onwards: the partitions in
    order, then each handler followed by its own segments. Blocks the previous
    table used and this one does not are cleared, so nothing reading the
    reserved area finds the remains of a partition that has been removed.
    """
    if disk.bad_block_list not in (0, END_OF_LIST) or disk.drive_init not in (0, END_OF_LIST):
        raise ConfigurationError(
            "This drive's partition table carries a bad-block list or drive "
            "initialisation code, which this build cannot rewrite safely."
        )
    block_size = reader.block_size
    per_cylinder = disk.blocks_per_cylinder
    if per_cylinder <= 0 or disk.cylinders <= 0:
        raise ConfigurationError("The drive geometry is not usable.")
    needed = table_blocks_needed(len(disk.partitions), disk.handlers)
    low_cylinder = disk.first_usable_cylinder
    limit = min(
        [low_cylinder * per_cylinder, reader.total_blocks]
        + [part.start_block for part in disk.partitions]
    )
    if disk.block + needed > limit:
        raise ConfigurationError(
            "The partition table and its filesystem handlers need "
            f"{needed} blocks, and only {max(0, limit - disk.block)} are reserved "
            "in front of the first partition."
        )
    for part in disk.partitions:
        if part.low_cylinder > part.high_cylinder or part.high_cylinder >= disk.cylinders:
            raise ConfigurationError(
                f"{part.name} does not fit on the drive's {disk.cylinders} cylinders."
            )
    ordered = sorted(disk.partitions, key=lambda part: part.low_cylinder)
    for before, after in zip(ordered, ordered[1:]):
        if after.low_cylinder <= before.high_cylinder:
            raise ConfigurationError(f"{before.name} and {after.name} overlap.")

    written: dict[int, bytes] = {}
    cursor = disk.block + 1
    part_numbers = list(range(cursor, cursor + len(disk.partitions)))
    cursor += len(disk.partitions)
    for index, part in enumerate(disk.partitions):
        following = part_numbers[index + 1] if index + 1 < len(part_numbers) else END_OF_LIST
        part.index = index
        part.block = part_numbers[index]
        written[part.block] = _partition_block(part, following, block_size, disk.host_id or HOST_ID)
    handler_numbers = []
    for handler in disk.handlers:
        handler_numbers.append(cursor)
        cursor += handler.blocks_needed
    for index, handler in enumerate(disk.handlers):
        following = (
            handler_numbers[index + 1] if index + 1 < len(handler_numbers) else END_OF_LIST
        )
        handler.block = handler_numbers[index]
        written.update(
            _handler_blocks(
                handler, handler.block, following, block_size, disk.host_id or HOST_ID
            )
        )
    highest = cursor - 1

    raw = bytearray(block_size)
    raw[0:4] = RDSK
    put_long(raw, 4, 64)
    put_long(raw, RDB_HOSTID, disk.host_id or HOST_ID)
    put_long(raw, RDB_BLOCKBYTES, disk.block_size or block_size)
    put_long(raw, RDB_FLAGS, disk.flags or 0x17)
    put_long(raw, RDB_BADBLOCKS, END_OF_LIST)
    put_long(raw, RDB_PARTITIONS, part_numbers[0] if part_numbers else END_OF_LIST)
    put_long(raw, RDB_FILESYSTEMS, handler_numbers[0] if handler_numbers else END_OF_LIST)
    put_long(raw, RDB_DRIVEINIT, END_OF_LIST)
    for offset in range(40, 64, 4):
        put_long(raw, offset, END_OF_LIST)
    put_long(raw, RDB_CYLINDERS, disk.cylinders)
    put_long(raw, RDB_SECTORS, disk.sectors)
    put_long(raw, RDB_HEADS, disk.heads)
    put_long(raw, RDB_INTERLEAVE, 1)
    put_long(raw, RDB_PARK, disk.cylinders)
    for offset in range(84, 96, 4):
        put_long(raw, offset, END_OF_LIST)
    put_long(raw, RDB_WRITEPRECOMP, disk.cylinders)
    put_long(raw, RDB_REDUCEDWRITE, disk.cylinders)
    put_long(raw, RDB_STEPRATE, 3)
    for offset in range(108, 128, 4):
        put_long(raw, offset, END_OF_LIST)
    put_long(raw, RDB_BLOCKSLO, disk.block)
    put_long(raw, RDB_BLOCKSHI, limit - 1)
    put_long(raw, RDB_LOCYLINDER, low_cylinder)
    put_long(raw, RDB_HICYLINDER, disk.cylinders - 1)
    put_long(raw, RDB_CYLBLOCKS, per_cylinder)
    put_long(raw, RDB_AUTOPARK, 0)
    put_long(raw, RDB_HIGHRDSK, highest)
    put_long(raw, 156, END_OF_LIST)
    raw[RDB_VENDOR : RDB_VENDOR + 8] = disk.disk_vendor.encode("latin-1", "replace")[:8].ljust(8, b" ")
    raw[RDB_PRODUCT : RDB_PRODUCT + 16] = disk.disk_product.encode("latin-1", "replace")[:16].ljust(16, b" ")
    raw[RDB_REVISION : RDB_REVISION + 4] = disk.disk_revision.encode("latin-1", "replace")[:4].ljust(4, b" ")
    apply_rdb_checksum(raw, 64)
    written[disk.block] = bytes(raw)

    blank = b"\0" * block_size
    for stale in disk.table_blocks:
        if stale not in written and disk.block <= stale < limit:
            reader.write_block(stale, blank)
    # The table's links are only followed once the RDSK block names them, so
    # it goes down last: a write interrupted before it leaves the old table.
    for number in sorted(written, reverse=True):
        reader.write_block(number, written[number])
    reader.flush()
    return read_rigid_disk(reader)


def new_partition(
    disk: RigidDisk,
    entry: dict,
    low_cylinder: int,
    high_cylinder: int,
    index: int = 0,
) -> Partition:
    """Describe a partition from a request, on the drive's own geometry."""
    flags = PARTF_BOOTABLE if entry.get("bootable") else 0
    if entry.get("automount") is False:
        flags |= PARTF_NOMOUNT
    dos_type = as_dos_type(entry.get("dosType"), int(entry.get("dosVariant", 3)))
    sectors_per_block = max(1, int(entry.get("sectorsPerBlock") or 1))
    return Partition(
        index=index,
        block=0,
        name=str(entry.get("name") or f"DH{index}")[:30],
        flags=flags,
        surfaces=disk.heads,
        blocks_per_track=disk.sectors,
        sectors_per_block=sectors_per_block,
        reserved=max(1, int(entry.get("reserved") or 2)),
        low_cylinder=low_cylinder,
        high_cylinder=high_cylinder,
        buffers=int(entry.get("buffers") or 30),
        boot_priority=int(entry.get("bootPriority") or 0),
        dos_type=dos_type,
        mask=int(entry.get("mask") or SAFE_MASK),
        max_transfer=int(entry.get("maxTransfer") or SAFE_MAX_TRANSFER),
        block_size=disk.block_size,
        preallocated=int(entry.get("preallocated") or 0),
    )


def plan_rigid_disk(
    total_blocks: int,
    partitions: list[dict],
    *,
    heads: int = 16,
    sectors: int = 63,
    block_size: int = BLOCK_SIZE,
    vendor: str = "AMIGA",
    product: str = "FILE FORGE HDF",
    revision: str = "1.1",
    handlers: list[FileSystemHandler] | None = None,
    scale_to_fit: bool = True,
) -> RigidDisk:
    """Lay partitions out across a drive without writing anything.

    ``partitions`` entries take ``name``, ``dosType``, ``sizeBytes`` or
    ``cylinders``, ``bootable``, ``bootPriority`` and ``buffers``. A partition
    with neither a size nor a cylinder count takes a share of whatever the
    sized ones leave, which is how a work partition soaks up the rest of a
    card. Sizes are rounded up to whole cylinders, because that is the only
    unit an RDB can describe.
    """
    handlers = list(handlers or [])
    blocks_per_cylinder = heads * sectors
    total_cylinders = total_blocks // blocks_per_cylinder
    # The first cylinder is kept for the RDB, its partition entries and room
    # to add more later, exactly as HDToolBox does. Handlers can need more.
    needed = table_blocks_needed(max(len(partitions), 8), handlers)
    reserved_cylinders = max(1, -(-needed // blocks_per_cylinder))
    available = total_cylinders - reserved_cylinders
    if available < max(1, len(partitions)):
        raise ConfigurationError("The image is too small for the requested partitions.")

    cylinder_bytes = blocks_per_cylinder * block_size
    requested: list[int | None] = []
    for entry in partitions:
        cylinders = int(entry.get("cylinders") or 0)
        if not cylinders:
            size = int(entry.get("sizeBytes") or 0)
            cylinders = -(-size // cylinder_bytes) if size > 0 else 0
        requested.append(cylinders or None)
    fixed = sum(value for value in requested if value)
    flexible = [index for index, value in enumerate(requested) if value is None]
    if flexible:
        if fixed + len(flexible) > available:
            raise ConfigurationError(
                "The partitions with a size leave no room for the ones that "
                "take the rest of the drive."
            )
        share, extra = divmod(available - fixed, len(flexible))
        for position, index in enumerate(flexible):
            requested[index] = share + (extra if position == len(flexible) - 1 else 0)
    elif fixed > available:
        if not scale_to_fit:
            raise ConfigurationError(
                "The partitions ask for more room than the drive has."
            )
        # Scale proportionally rather than refusing, so "split this drive in
        # four" always produces four usable partitions.
        scale = available / fixed
        requested = [max(1, int(value * scale)) for value in requested]
        while sum(requested) > available:
            requested[requested.index(max(requested))] -= 1

    disk = RigidDisk(
        block=0,
        block_size=block_size,
        cylinders=total_cylinders,
        sectors=sectors,
        heads=heads,
        high_rdb_block=0,
        park_cylinder=total_cylinders,
        disk_vendor=vendor,
        disk_product=product,
        disk_revision=revision,
        handlers=handlers,
        low_cylinder=reserved_cylinders,
        high_cylinder=total_cylinders - 1,
    )
    low = reserved_cylinders
    for index, (entry, cylinders) in enumerate(zip(partitions, requested)):
        high = low + int(cylinders) - 1
        disk.partitions.append(new_partition(disk, entry, low, high, index))
        low = high + 1
    return disk


def write_rigid_disk(
    reader: BlockReader,
    partitions: list[dict],
    *,
    heads: int = 16,
    sectors: int = 63,
    block_size: int = BLOCK_SIZE,
    vendor: str = "AMIGA",
    product: str = "FILE FORGE HDF",
    revision: str = "1.1",
    first_partition_block: int = 1,
    handlers: list[FileSystemHandler] | None = None,
    scale_to_fit: bool = True,
) -> RigidDisk:
    """Lay out a fresh RDB and its partition chain across the whole image."""
    if not partitions:
        raise ConfigurationError("An RDB needs at least one partition.")
    if first_partition_block != 1:
        raise ConfigurationError("Partition blocks follow the RDSK block directly.")
    disk = plan_rigid_disk(
        reader.total_blocks,
        partitions,
        heads=heads,
        sectors=sectors,
        block_size=block_size,
        vendor=vendor,
        product=product,
        revision=revision,
        handlers=handlers,
        scale_to_fit=scale_to_fit,
    )
    # Whatever described the drive before goes first: the old table, or the
    # boot block of a volume that started at block zero.
    blank = b"\0" * reader.block_size
    for block in range(min(reader.total_blocks, RDB_SEARCH_LIMIT)):
        reader.write_block(block, blank)
    return store_rigid_disk(reader, disk)


# ---------------------------------------------------------------------------
# Changing a table that is already there
# ---------------------------------------------------------------------------
def extend_to_media(reader: BlockReader) -> RigidDisk:
    """Let the table describe the whole of the drive it now sits on.

    An image written to a larger card still says it is the size it was made
    at, so the rest of the card is out of reach. Raising the cylinder count
    puts that room at the end of the drive, where a partition can be added.
    Nothing already on the drive moves.
    """
    disk = read_rigid_disk(reader)
    cylinders = reader.total_blocks // disk.blocks_per_cylinder
    if cylinders <= disk.cylinders:
        return disk
    disk.cylinders = cylinders
    disk.high_cylinder = cylinders - 1
    return store_rigid_disk(reader, disk)


def add_partition(reader: BlockReader, entry: dict) -> Partition:
    """Add a partition in unused space and return it as the drive now lists it.

    ``lowCylinder`` chooses which run of free cylinders to use and defaults to
    the largest. A partition with no size takes the whole run.
    """
    disk = read_rigid_disk(reader)
    ranges = disk.free_ranges()
    if not ranges:
        raise ConfigurationError("Every cylinder of this drive belongs to a partition already.")
    wanted = entry.get("lowCylinder")
    if wanted is None:
        low, high = max(ranges, key=lambda pair: pair[1] - pair[0])
    else:
        wanted = int(wanted)
        chosen = [pair for pair in ranges if pair[0] <= wanted <= pair[1]]
        if not chosen:
            raise ConfigurationError(f"Cylinder {wanted} is not free.")
        low, high = wanted, chosen[0][1]
    cylinders = int(entry.get("cylinders") or 0)
    if not cylinders and int(entry.get("sizeBytes") or 0) > 0:
        cylinders = -(-int(entry["sizeBytes"]) // disk.cylinder_bytes)
    if cylinders:
        if cylinders > high - low + 1:
            raise ConfigurationError(
                "There is not that much unused room at that place on the drive."
            )
        high = low + cylinders - 1
    name = str(entry.get("name") or "").strip()
    taken = {part.name.lower() for part in disk.partitions}
    if not name:
        number = 0
        while f"dh{number}" in taken:
            number += 1
        name = f"DH{number}"
    if name.lower() in taken:
        raise ConfigurationError(f"This drive already has a partition called {name}.")
    created = new_partition(disk, {**entry, "name": name}, low, high, len(disk.partitions))
    disk.partitions.append(created)
    stored = store_rigid_disk(reader, disk)
    return next(part for part in stored.partitions if part.name == created.name)


def remove_partition(reader: BlockReader, index: int) -> RigidDisk:
    """Take a partition out of the table, leaving its cylinders unused.

    The volume's own blocks are not touched. Until something else is put
    there, adding a partition over the same cylinders with the same filing
    system brings the volume back.
    """
    disk = read_rigid_disk(reader)
    if not 0 <= int(index) < len(disk.partitions):
        raise ConfigurationError(f"Partition {index} does not exist on this drive.")
    del disk.partitions[int(index)]
    return store_rigid_disk(reader, disk)


def change_partition(reader: BlockReader, index: int, changes: dict) -> Partition:
    """Change how a partition is described without moving or resizing it."""
    disk = read_rigid_disk(reader)
    if not 0 <= int(index) < len(disk.partitions):
        raise ConfigurationError(f"Partition {index} does not exist on this drive.")
    part = disk.partitions[int(index)]
    if "name" in changes:
        name = str(changes["name"] or "").strip()
        if not name:
            raise ConfigurationError("A partition needs a device name.")
        others = {
            other.name.lower() for other in disk.partitions if other is not part
        }
        if name.lower() in others:
            raise ConfigurationError(f"This drive already has a partition called {name}.")
        part.name = name[:30]
    if "bootable" in changes:
        part.flags = (part.flags & ~PARTF_BOOTABLE) | (
            PARTF_BOOTABLE if changes["bootable"] else 0
        )
    if "automount" in changes:
        part.flags = (part.flags & ~PARTF_NOMOUNT) | (
            0 if changes["automount"] else PARTF_NOMOUNT
        )
    if "bootPriority" in changes:
        part.boot_priority = max(-128, min(127, int(changes["bootPriority"])))
    if "dosType" in changes:
        part.dos_type = as_dos_type(changes["dosType"])
    for key, attribute in (
        ("buffers", "buffers"),
        ("maxTransfer", "max_transfer"),
        ("mask", "mask"),
        ("reserved", "reserved"),
        ("sectorsPerBlock", "sectors_per_block"),
        ("preallocated", "preallocated"),
    ):
        if key in changes and changes[key] is not None:
            setattr(part, attribute, int(changes[key]))
    stored = store_rigid_disk(reader, disk)
    return stored.partitions[int(index)]


def set_handler(reader: BlockReader, handler: FileSystemHandler) -> RigidDisk:
    """Put a handler in the table, replacing any the drive has for that DOS type."""
    disk = read_rigid_disk(reader)
    disk.handlers = [
        existing for existing in disk.handlers if existing.dos_type != handler.dos_type
    ]
    disk.handlers.append(handler)
    return store_rigid_disk(reader, disk)


def remove_handler(reader: BlockReader, dos_type: bytes) -> RigidDisk:
    """Take the handler for one DOS type out of the table."""
    disk = read_rigid_disk(reader)
    kept = [handler for handler in disk.handlers if handler.dos_type != dos_type]
    if len(kept) == len(disk.handlers):
        raise ConfigurationError("This drive carries no handler for that filing system.")
    disk.handlers = kept
    return store_rigid_disk(reader, disk)


__all__ = [
    "FileSystemHandler",
    "PARTF_BOOTABLE",
    "PARTF_NOMOUNT",
    "Partition",
    "RDB_SEARCH_LIMIT",
    "RigidDisk",
    "SAFE_MASK",
    "SAFE_MAX_TRANSFER",
    "add_partition",
    "as_dos_type",
    "change_partition",
    "extend_to_media",
    "find_rdb_block",
    "geometry_for_drive",
    "handler_version",
    "new_partition",
    "partition_reader",
    "plan_rigid_disk",
    "read_rigid_disk",
    "remove_handler",
    "remove_partition",
    "set_handler",
    "store_rigid_disk",
    "table_blocks_needed",
    "write_rigid_disk",
]
