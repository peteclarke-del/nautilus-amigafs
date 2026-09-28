"""Raw block access and the AmigaDOS on-disk block formats.

Everything here works in whole 512-byte blocks of big-endian longs, which is
what the Amiga's ``trackdisk.device`` and ``scsi.device`` hand to the
filesystem. Keeping the structure decoding in one module means the volume
code above it never touches an offset directly.
"""

from __future__ import annotations

import os
import stat
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import DataError

BLOCK_SIZE = 512
LONGS_PER_BLOCK = BLOCK_SIZE // 4

# Primary block types.
T_HEADER = 2
T_DATA = 8
T_LIST = 16
T_DIRCACHE = 33
T_COMMENT = 64

# Secondary block types.
ST_ROOT = 1
ST_USERDIR = 2
ST_SOFTLINK = 3
ST_LINKDIR = 4
ST_FILE = -3
ST_LINKFILE = -4

# Standard floppy geometries, in blocks.
DD_BLOCKS = 80 * 2 * 11        # 1760 blocks, 880 KiB
HD_BLOCKS = 80 * 2 * 22        # 3520 blocks, 1.76 MiB
RESERVED_BLOCKS = 2            # the two boot blocks

# DOS types. The trailing byte selects the variant.
DOS_TYPES = {
    b"DOS\x00": "OFS",
    b"DOS\x01": "FFS",
    b"DOS\x02": "OFS-INTL",
    b"DOS\x03": "FFS-INTL",
    b"DOS\x04": "OFS-DC",
    b"DOS\x05": "FFS-DC",
    b"DOS\x06": "OFS-LNFS",
    b"DOS\x07": "FFS-LNFS",
    b"PFS\x01": "PFS3",
    b"PFS\x02": "PFS3",
    b"PFS\x03": "PFS3",
    b"PDS\x03": "PFS3",
    b"SFS\x00": "SFS",
    b"SFS\x02": "SFS2",
}

#: The DOS type to write for each label. PFS3 has several spellings, and a
#: partition for the PFS3 handler is normally declared ``PFS\3``.
FORMAT_LABELS = {value: key for key, value in reversed(DOS_TYPES.items())}
FORMAT_LABELS["PFS3"] = b"PFS\x03"

#: AmigaDOS variants the OFS/FFS driver can read and write. SFS and PFS3 are
#: read and written too, by their own drivers, so they are not listed here:
#: this list also decides which DOS types the FFS formatter will lay down.
WRITABLE_FORMATS = (
    "OFS",
    "FFS",
    "OFS-INTL",
    "FFS-INTL",
    "OFS-DC",
    "FFS-DC",
    "OFS-LNFS",
    "FFS-LNFS",
)

#: Variants this build can name but has no driver for.
READ_ONLY_FORMATS = ("SFS2",)

MAX_NAME = 30
MAX_COMMENT = 79

#: The long-filename variants allow names this long. Name and comment share
#: one 112-byte area of the header, each with its own length byte.
MAX_LONG_NAME = 107
LONG_NAME_AREA = 112


def is_ffs(dos_type: bytes) -> bool:
    """FFS stores file data in whole blocks; OFS reserves a 24-byte header."""
    return bool(dos_type[3] & 1)


def is_international(dos_type: bytes) -> bool:
    """International mode folds the 8-bit Latin-1 letters when hashing.

    ``DOS\\2`` to ``DOS\\7`` all hash this way. The directory-cache and
    long-name variants have no bit of their own for it: international mode
    is implied by both.
    """
    return dos_type[:3] == b"DOS" and 2 <= dos_type[3] <= 7


def is_dircache(dos_type: bytes) -> bool:
    """Directory-cache mode keeps a summary block chain for fast listings.

    Only ``DOS\\4`` and ``DOS\\5`` use it. ``DOS\\6`` and ``DOS\\7`` also
    have bit 2 set, but there it means long file names, not a cache.
    """
    return dos_type[:3] == b"DOS" and dos_type[3] in (4, 5)


def is_long_names(dos_type: bytes) -> bool:
    """The FFS of AmigaOS 3.1.4 and 3.2 stores names of up to 107 characters."""
    return dos_type[:3] == b"DOS" and dos_type[3] in (6, 7)


def upper_char(character: str, international: bool) -> str:
    """Fold one character exactly the way AmigaDOS hashing does."""
    code = ord(character)
    if international:
        if 97 <= code <= 122 or 224 <= code <= 254 and code != 247:
            return chr(code - 32)
        return character
    if 97 <= code <= 122:
        return chr(code - 32)
    return character


def hash_name(name: str, international: bool, table_size: int) -> int:
    """Return the hash-table slot AmigaDOS would use for this name."""
    value = len(name)
    for character in name:
        value = (value * 13 + ord(upper_char(character, international))) & 0x7FF
    return value % table_size


def names_match(left: str, right: str, international: bool) -> bool:
    """AmigaDOS name comparison: case-insensitive, with the same folding."""
    if len(left) != len(right):
        return False
    return all(
        upper_char(a, international) == upper_char(b, international)
        for a, b in zip(left, right)
    )


def block_checksum(block: bytes, checksum_offset: int = 20) -> int:
    """Return the value that makes the block's longs sum to zero."""
    total = 0
    for index in range(0, len(block), 4):
        if index == checksum_offset:
            continue
        (value,) = struct.unpack_from(">I", block, index)
        total = (total + value) & 0xFFFFFFFF
    return (-total) & 0xFFFFFFFF


def apply_checksum(block: bytearray, checksum_offset: int = 20) -> bytearray:
    struct.pack_into(">I", block, checksum_offset, block_checksum(bytes(block), checksum_offset))
    return block


def verify_checksum(block: bytes, checksum_offset: int = 20) -> bool:
    (stored,) = struct.unpack_from(">I", block, checksum_offset)
    return stored == block_checksum(block, checksum_offset)


def read_bstr(block: bytes, offset: int, limit: int) -> str:
    """Read a BCPL string: one length byte followed by its characters."""
    length = min(block[offset], limit)
    return block[offset + 1 : offset + 1 + length].decode("latin-1")


def write_bstr(block: bytearray, offset: int, value: str, limit: int) -> None:
    encoded = value.encode("latin-1", "replace")[:limit]
    block[offset] = len(encoded)
    block[offset + 1 : offset + 1 + limit] = encoded.ljust(limit, b"\0")


def long_at(block: bytes, offset: int) -> int:
    (value,) = struct.unpack_from(">I", block, offset)
    return value


def signed_long_at(block: bytes, offset: int) -> int:
    (value,) = struct.unpack_from(">i", block, offset)
    return value


def put_long(block: bytearray, offset: int, value: int) -> None:
    struct.pack_into(">I", block, offset, int(value) & 0xFFFFFFFF)


def put_signed_long(block: bytearray, offset: int, value: int) -> None:
    struct.pack_into(">i", block, offset, int(value))


@dataclass(frozen=True)
class DirCacheRecord:
    """One entry in a ``DOS\\4`` or ``DOS\\5`` directory-cache block.

    The cache repeats what ``Examine`` needs from each header block of a
    directory, so that ``List`` and Workbench read a handful of cache blocks
    instead of every header. Each record is packed as the header key, size
    and protection as longs; owner and group, then the date's days, minutes
    and ticks as words; the secondary type as one byte; then the name and the
    comment, each with a length byte. A record starts on an even offset, so an
    odd total is padded with one zero byte.
    """

    header: int
    size: int
    protection: int
    uid: int
    gid: int
    days: int
    mins: int
    ticks: int
    secondary_type: int
    name: bytes
    comment: bytes

    FIXED = 25

    @property
    def packed_size(self) -> int:
        return (self.FIXED + len(self.name) + len(self.comment) + 1) & ~1

    def pack(self) -> bytes:
        body = struct.pack(
            ">IIIHHHHHbB",
            self.header & 0xFFFFFFFF,
            self.size & 0xFFFFFFFF,
            self.protection & 0xFFFFFFFF,
            self.uid & 0xFFFF,
            self.gid & 0xFFFF,
            self.days & 0xFFFF,
            self.mins & 0xFFFF,
            self.ticks & 0xFFFF,
            self.secondary_type,
            len(self.name),
        )
        body += self.name + bytes([len(self.comment)]) + self.comment
        return body.ljust(self.packed_size, b"\0")


def unpack_dircache_records(block: bytes, count: int) -> list[DirCacheRecord]:
    """Decode ``count`` records from a directory-cache block.

    A count or length that would run past the end of the block means the
    cache is damaged, and is reported rather than read as garbage.
    """
    records: list[DirCacheRecord] = []
    offset = 24
    for _ in range(count):
        if offset + DirCacheRecord.FIXED > len(block):
            raise DataError("A directory-cache block holds more records than fit in it.")
        fields = struct.unpack_from(">IIIHHHHHbB", block, offset)
        name_start = offset + 24
        name = block[name_start : name_start + fields[9]]
        comment_length_at = name_start + fields[9]
        if comment_length_at >= len(block):
            raise DataError("A directory-cache record runs past the end of its block.")
        comment_length = block[comment_length_at]
        comment = block[comment_length_at + 1 : comment_length_at + 1 + comment_length]
        record = DirCacheRecord(*fields[:9], name=name, comment=comment)
        if offset + record.packed_size > len(block):
            raise DataError("A directory-cache record runs past the end of its block.")
        records.append(record)
        offset += record.packed_size
    return records


def pack_dircache_records(records, block_size: int = BLOCK_SIZE) -> bytes:
    """Return the record area of a cache block, 24 bytes short of a block."""
    area = b"".join(record.pack() for record in records)
    if len(area) > block_size - 24:
        raise DataError("Too many directory-cache records for one block.")
    return area.ljust(block_size - 24, b"\0")


def media_size(handle) -> int:
    """Return the size in bytes of an open image file or block device.

    A regular file reports its length through ``stat``. A block device, such
    as a drive taken from an Amiga and attached through a USB adapter, reports
    a length of zero there, so its capacity is found by seeking to the end.
    """
    details = os.fstat(handle.fileno())
    if not stat.S_ISBLK(details.st_mode):
        return details.st_size
    position = handle.tell()
    try:
        return handle.seek(0, os.SEEK_END)
    finally:
        handle.seek(position)


class BlockReader:
    """A seekable window onto an image file, addressed in whole blocks."""

    def __init__(
        self,
        path: Path | str,
        *,
        writable: bool = False,
        offset: int = 0,
        length: int | None = None,
        block_size: int = BLOCK_SIZE,
    ):
        self.path = Path(path)
        self.writable = bool(writable)
        self.block_size = int(block_size)
        self._handle = self.path.open("r+b" if writable else "rb")
        size = media_size(self._handle)
        self.offset = int(offset)
        if self.offset < 0 or self.offset > size:
            raise DataError("The partition starts beyond the end of the image.")
        available = size - self.offset
        self.length = int(length) if length is not None else available
        if self.length > available:
            # A partition table may describe a drive larger than the file that
            # holds it. Report the honest usable extent rather than reading
            # past the end of the file.
            self.length = available
        self.total_blocks = self.length // self.block_size

    # ---- context management ------------------------------------------
    def __enter__(self) -> "BlockReader":
        return self

    def __exit__(self, *_exception) -> None:
        self.close()

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.flush()
            self._handle.close()

    # ---- block access ------------------------------------------------
    def read_block(self, number: int) -> bytes:
        if not 0 <= number < self.total_blocks:
            raise DataError(f"Block {number} is outside this volume.")
        self._handle.seek(self.offset + number * self.block_size)
        data = self._handle.read(self.block_size)
        if len(data) < self.block_size:
            data = data.ljust(self.block_size, b"\0")
        return data

    def read_range(self, offset: int, length: int) -> bytes:
        """Read ``length`` bytes starting ``offset`` bytes into this window.

        A filing system with its own block size, or one reading a long run of
        file data, asks for the whole range at once rather than block by block.
        """
        if offset < 0 or length < 0 or offset + length > self.length:
            raise DataError("The requested range is outside this volume.")
        self._handle.seek(self.offset + offset)
        data = self._handle.read(length)
        if len(data) < length:
            data = data.ljust(length, b"\0")
        return data

    def write_block(self, number: int, data: bytes) -> None:
        if not self.writable:
            raise DataError("This volume is open read-only.")
        if not 0 <= number < self.total_blocks:
            raise DataError(f"Block {number} is outside this volume.")
        if len(data) != self.block_size:
            raise DataError("A block write must supply exactly one block.")
        self._handle.seek(self.offset + number * self.block_size)
        self._handle.write(data)

    def write_range(self, offset: int, data: bytes) -> None:
        """Write bytes starting ``offset`` bytes into this window."""
        if not self.writable:
            raise DataError("This volume is open read-only.")
        if offset < 0 or offset + len(data) > self.length:
            raise DataError("The requested range is outside this volume.")
        self._handle.seek(self.offset + offset)
        self._handle.write(data)

    def flush(self) -> None:
        if self.writable:
            self._handle.flush()

    def sync(self) -> None:
        """Push every write so far to the medium before anything that depends on it.

        A filing system that orders its writes for crash safety needs each
        stage on the disk, not in a cache, before the next one begins.
        """
        if self.writable:
            self._handle.flush()
            os.fsync(self._handle.fileno())

    def reopen(
        self,
        *,
        writable: bool | None = None,
        offset: int | None = None,
        length: int | None = None,
        block_size: int | None = None,
    ) -> "BlockReader":
        """Open another reader onto the same medium.

        ``offset`` is absolute, in bytes from the start of the medium. Every
        driver that needs a second view of its medium asks for it here, so a
        reader backed by something other than a named file can supply a view
        of the same backing store.
        """
        return BlockReader(
            self.path,
            writable=self.writable if writable is None else writable,
            offset=self.offset if offset is None else offset,
            length=self.length if length is None else length,
            block_size=self.block_size if block_size is None else block_size,
        )

    def read_all(self) -> bytes:
        """Return every byte of this window."""
        return self.read_range(0, self.length)

    def window(self, offset_blocks: int, length_blocks: int) -> "BlockReader":
        """Open a nested reader for one partition of this device."""
        return self.reopen(
            offset=self.offset + offset_blocks * self.block_size,
            length=length_blocks * self.block_size,
        )


@dataclass
class Geometry:
    """Physical geometry for an image that does not carry its own."""

    surfaces: int = 2
    blocks_per_track: int = 11
    reserved: int = RESERVED_BLOCKS
    block_size: int = BLOCK_SIZE
    low_cylinder: int = 0
    high_cylinder: int = 79
    sectors_per_block: int = 1
    boot_priority: int = 0
    dos_type: bytes = b"DOS\x00"
    mask: int = 0x7FFFFFFE
    max_transfer: int = 0x00FFFFFF
    buffers: int = 30
    label: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def cylinders(self) -> int:
        return self.high_cylinder - self.low_cylinder + 1

    @property
    def total_blocks(self) -> int:
        return self.cylinders * self.surfaces * self.blocks_per_track

    @property
    def size_bytes(self) -> int:
        return self.total_blocks * self.block_size

    def to_dict(self) -> dict:
        return {
            "surfaces": self.surfaces,
            "blocksPerTrack": self.blocks_per_track,
            "reserved": self.reserved,
            "blockSize": self.block_size,
            "lowCylinder": self.low_cylinder,
            "highCylinder": self.high_cylinder,
            "cylinders": self.cylinders,
            "totalBlocks": self.total_blocks,
            "sizeBytes": self.size_bytes,
            "bootPriority": self.boot_priority,
            "dosType": self.dos_type.decode("latin-1"),
            "format": DOS_TYPES.get(self.dos_type, "unknown"),
            "label": self.label,
        }


DD_GEOMETRY = Geometry(surfaces=2, blocks_per_track=11, high_cylinder=79)
HD_GEOMETRY = Geometry(surfaces=2, blocks_per_track=22, high_cylinder=79)

#: Named floppy and drive geometries the workbench can create.
NAMED_GEOMETRIES = {
    "dd": DD_GEOMETRY,
    "880k": DD_GEOMETRY,
    "hd": HD_GEOMETRY,
    "1760k": HD_GEOMETRY,
}


__all__ = [
    "BLOCK_SIZE",
    "BlockReader",
    "DD_BLOCKS",
    "DD_GEOMETRY",
    "DOS_TYPES",
    "FORMAT_LABELS",
    "Geometry",
    "HD_BLOCKS",
    "HD_GEOMETRY",
    "DirCacheRecord",
    "LONGS_PER_BLOCK",
    "LONG_NAME_AREA",
    "MAX_COMMENT",
    "MAX_LONG_NAME",
    "MAX_NAME",
    "NAMED_GEOMETRIES",
    "READ_ONLY_FORMATS",
    "RESERVED_BLOCKS",
    "ST_FILE",
    "ST_LINKDIR",
    "ST_LINKFILE",
    "ST_ROOT",
    "ST_SOFTLINK",
    "ST_USERDIR",
    "T_COMMENT",
    "T_DATA",
    "T_DIRCACHE",
    "T_HEADER",
    "T_LIST",
    "WRITABLE_FORMATS",
    "apply_checksum",
    "block_checksum",
    "hash_name",
    "is_dircache",
    "is_ffs",
    "is_international",
    "is_long_names",
    "pack_dircache_records",
    "unpack_dircache_records",
    "long_at",
    "media_size",
    "names_match",
    "put_long",
    "put_signed_long",
    "read_bstr",
    "signed_long_at",
    "upper_char",
    "verify_checksum",
    "write_bstr",
]
