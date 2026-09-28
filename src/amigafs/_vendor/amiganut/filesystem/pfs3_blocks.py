"""The Professional File System's on-disk structures, below the volume level.

Everything here is a pure function of bytes: the block identifiers, the
option bits, the layout of a directory entry and its packed extra fields,
international name folding and the sizing rules the formatter follows.
``pfs3`` reads volumes with these and ``pfs3_write`` changes them, so both
agree on every byte by construction.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from ..errors import DataError
from ..file import datestamp_to_datetime, datetime_to_datestamp
from .blocks import ST_LINKDIR, ST_LINKFILE, ST_ROOT, ST_SOFTLINK, ST_USERDIR

#: What a root block or boot block carries. ``PFS\\2`` marks the newer layout
#: with reserved blocks larger than 1 KB or logical blocks larger than 512
#: bytes; the structures are otherwise the same.
PFS1_ID = b"PFS\x01"
PFS2_ID = b"PFS\x02"
ROOT_IDS = (PFS1_ID, PFS2_ID)

#: Dos types a partition table uses for PFS3. They name the handler, not the
#: layout, so any of them may hold a volume whose root says ``PFS\\1``.
PFS3_DOS_TYPES = (b"PFS\x01", b"PFS\x02", b"PFS\x03", b"PDS\x03")

# Block identifiers, the first word of every reserved block.
DIRBLOCK_ID = b"DB"
ANODEBLOCK_ID = b"AB"
INDEXBLOCK_ID = b"IB"
BITMAPBLOCK_ID = b"BM"
BITMAPINDEX_ID = b"MI"
DELDIR_ID = b"DD"
EXTENSION_ID = b"EX"
SUPERBLOCK_ID = b"SB"

# Root block option bits.
MODE_HARDDISK = 1
MODE_SPLITTED_ANODES = 2
MODE_DIR_EXTENSION = 4
MODE_DELDIR = 8
MODE_SIZEFIELD = 16
MODE_EXTENSION = 32
MODE_DATESTAMP = 64
MODE_SUPERINDEX = 128
MODE_SUPERDELDIR = 256
MODE_EXTROVING = 512
MODE_LONGFN = 1024
MODE_LARGEFILE = 2048
MODE_STORED_GEOM = 4096
KNOWN_MODES = 8191

MODE_NAMES = (
    (MODE_HARDDISK, "HARDDISK"),
    (MODE_SPLITTED_ANODES, "SPLITTED_ANODES"),
    (MODE_DIR_EXTENSION, "DIR_EXTENSION"),
    (MODE_DELDIR, "DELDIR"),
    (MODE_SIZEFIELD, "SIZEFIELD"),
    (MODE_EXTENSION, "EXTENSION"),
    (MODE_DATESTAMP, "DATESTAMP"),
    (MODE_SUPERINDEX, "SUPERINDEX"),
    (MODE_SUPERDELDIR, "SUPERDELDIR"),
    (MODE_EXTROVING, "EXTROVING"),
    (MODE_LONGFN, "LONGFN"),
    (MODE_LARGEFILE, "LARGEFILE"),
    (MODE_STORED_GEOM, "STORED_GEOM"),
)

# Fixed places.
BOOTBLOCK = 0
ROOTBLOCK = 2
#: The root block structure is 512 bytes whatever the block size, and the
#: reserved bitmap starts immediately after it inside the root cluster.
ROOT_SIZE = 512
RESERVED_BITMAP = ROOT_SIZE + 12

# Root block fields.
ROOT_OPTIONS = 4
ROOT_DATESTAMP = 8
ROOT_CREATION = 12
ROOT_PROTECTION = 18
ROOT_DISKNAME = 20
ROOT_LASTRESERVED = 52
ROOT_FIRSTRESERVED = 56
ROOT_RESERVED_FREE = 60
ROOT_RESERVED_BLKSIZE = 64
ROOT_RBLKCLUSTER = 66
ROOT_BLOCKSFREE = 68
ROOT_ALWAYSFREE = 72
ROOT_ROVING_PTR = 76
ROOT_DELDIR = 80
ROOT_DISKSIZE = 84
ROOT_EXTENSION = 88
ROOT_BITMAPINDEX = 96
#: Small volumes list five bitmap index blocks and then 99 anode index
#: blocks; volumes in super index mode give all 104 slots to the bitmap.
ROOT_SMALL_INDEX = ROOT_BITMAPINDEX + 5 * 4
MAXSMALLBITMAPINDEX = 4
MAXBITMAPINDEX = 103
MAXSMALLINDEXNR = 98
MAXSUPER = 15
MAXNUMRESERVED = 4096 + 255 * 1024 * 8

# Root block extension fields.
EXT_OPTIONS = 4
EXT_DATESTAMP = 8
EXT_VERSION = 12
EXT_ROOT_DATE = 16
EXT_VOLUME_DATE = 22
EXT_TOBEDONE = 28
EXT_RESERVED_ROVING = 44
EXT_ROVINGBIT = 48
EXT_CURRANSEQNR = 50
EXT_DELDIRROVING = 52
EXT_DELDIRSIZE = 54
EXT_FNSIZE = 56
EXT_SUPERINDEX = 64
EXT_DD_PROTECTION = 132
EXT_DD_DATE = 136
EXT_DELDIR = 144

#: Postponed operations the handler records in the extension before a long
#: free, so that it can finish one interrupted by a crash.
POSTPONED_OPERATIONS = {
    1: "free the blocks of a deleted file",
    2: "free the blocks of a file moved to the deleted-files directory",
    3: "free the anodes of an entry dropped from the deleted-files directory",
}

# Header sizes of the reserved block types.
BLOCK_HEADER = 12
ANODEBLOCK_HEADER = 16
DIRBLOCK_HEADER = 20
DELDIRBLOCK_HEADER = 32
DELDIR_ENTRY = 32
DELENTRIES_PER_BLOCK = 31
ANODE_SIZE = 12

# Anodes 0 to 4 are reserved and the root directory is always anode 5.
ANODE_EOF = 0
ANODE_BADBLOCKS = 4
ANODE_ROOTDIR = 5
ANODE_USERFIRST = 6
#: The last few anodes of each anode block are kept back so that a file can
#: grow a chained anode in the same block as the one before it.
RESERVEDANODES = 6
#: An allocated anode that describes no blocks yet: an empty file, or one of
#: the reserved anodes 0 to 4.
EMPTY_BLOCKNR = 0xFFFFFFFF

ST_ROLLOVERFILE = -16
FIBF_ARCHIVE = 0x10
FIBF_DELETE = 0x01

#: Directory entry fields.
ENTRY_FIXED = 18
DIRENTRY_SIZE = 20
MAX_ENTRY = 255
MAX_COMMENT = 79
#: The name field is sized by the volume's ``fnsize``, 32 unless raised.
DEFAULT_FNSIZE = 32
MAX_FNSIZE = 107
MAX_DISKNAME = 30

#: The handler keeps this many reserved blocks free as a working margin.
RESFREE_THRESHOLD = 10

# Disk size limits, in 512-byte sectors.
BITMAP_PAYLOAD_1K = 1024 // 4 - 3
BITMAP_PAYLOAD_2K = 2048 // 4 - 3
BITMAP_PAYLOAD_4K = 4096 // 4 - 3
MAXSMALLDISK = (MAXSMALLBITMAPINDEX + 1) * BITMAP_PAYLOAD_1K * BITMAP_PAYLOAD_1K * 32
MAXDISKSIZE1K = (MAXBITMAPINDEX + 1) * BITMAP_PAYLOAD_1K * BITMAP_PAYLOAD_1K * 32
MAXDISKSIZE2K = (MAXBITMAPINDEX + 1) * BITMAP_PAYLOAD_2K * BITMAP_PAYLOAD_2K * 32
MAXDISKSIZE4K = (MAXBITMAPINDEX + 1) * BITMAP_PAYLOAD_4K * BITMAP_PAYLOAD_4K * 32

#: The version the formatter records, matching pfs3aio 19.2.
VERNUM, REVNUM = 19, 2

_U16 = struct.Struct(">H")
_U32 = struct.Struct(">I")
_ANODE = struct.Struct(">III")
_NONZERO = re.compile(rb"[^\x00]")


def u16(data, offset: int) -> int:
    return _U16.unpack_from(data, offset)[0]


def u32(data, offset: int) -> int:
    return _U32.unpack_from(data, offset)[0]


def put_u16(data: bytearray, offset: int, value: int) -> None:
    _U16.pack_into(data, offset, int(value) & 0xFFFF)


def put_u32(data: bytearray, offset: int, value: int) -> None:
    _U32.pack_into(data, offset, int(value) & 0xFFFFFFFF)


def unpack_anode(data, offset: int) -> tuple[int, int, int]:
    return _ANODE.unpack_from(data, offset)


def pack_anode(data: bytearray, offset: int, clustersize: int, blocknr: int, following: int) -> None:
    _ANODE.pack_into(data, offset, clustersize & 0xFFFFFFFF, blocknr & 0xFFFFFFFF, following & 0xFFFFFFFF)


def mode_names(options: int) -> list[str]:
    return [name for value, name in MODE_NAMES if options & value]


def first_set_bit(data, start: int, stop: int) -> int:
    """Return the first set bit at or after ``start`` and before ``stop``.

    PFS3 bitmaps are big-endian longs read most significant bit first, so a
    bitmap is simply a bit string in byte order and bit ``n`` is the mask
    ``0x80 >> (n % 8)`` of byte ``n // 8``. Returns -1 when none is set.
    """
    if start >= stop:
        return -1
    byte = start // 8
    head = data[byte] & (0xFF >> (start % 8))
    if head:
        found = byte * 8 + 8 - head.bit_length()
        return found if found < stop else -1
    match = _NONZERO.search(data, byte + 1, (stop + 7) // 8)
    if match is None:
        return -1
    position = match.start()
    found = position * 8 + 8 - data[position].bit_length()
    return found if found < stop else -1


def first_clear_bit(data, start: int, stop: int) -> int:
    """Return the first clear bit at or after ``start``, or ``stop`` if none."""
    position = start
    while position < stop:
        byte = position // 8
        value = data[byte] | (0xFF00 >> (position % 8)) & 0xFF
        if value != 0xFF:
            inverted = ~value & 0xFF
            found = byte * 8 + 8 - inverted.bit_length()
            return min(found, stop)
        position = (byte + 1) * 8
        # Skip whole runs of set bytes quickly.
        while position + 64 <= stop and data[position // 8 : position // 8 + 8] == b"\xff" * 8:
            position += 64
    return stop


def set_bits(data: bytearray, start: int, count: int, value: bool) -> None:
    """Set or clear ``count`` bits of a bitmap from bit ``start``."""
    end = start + count
    position = start
    while position < end and position % 8:
        mask = 0x80 >> (position % 8)
        data[position // 8] = (data[position // 8] | mask) if value else (data[position // 8] & ~mask)
        position += 1
    whole = (end - position) // 8
    if whole > 0:
        data[position // 8 : position // 8 + whole] = (b"\xff" if value else b"\x00") * whole
        position += whole * 8
    while position < end:
        mask = 0x80 >> (position % 8)
        data[position // 8] = (data[position // 8] | mask) if value else (data[position // 8] & ~mask)
        position += 1


def count_set_bits(data, start: int, stop: int) -> int:
    """Count the set bits in ``[start, stop)``."""
    if stop <= start:
        return 0
    first_byte, last_byte = start // 8, (stop - 1) // 8
    if first_byte == last_byte:
        value = data[first_byte] & (0xFF >> (start % 8)) & (0xFF << (7 - (stop - 1) % 8))
        return bin(value).count("1")
    total = bin(data[first_byte] & (0xFF >> (start % 8))).count("1")
    total += int.from_bytes(data[first_byte + 1 : last_byte], "big").bit_count()
    total += bin(data[last_byte] & (0xFF << (7 - (stop - 1) % 8)) & 0xFF).count("1")
    return total


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def fold(raw: bytes) -> bytes:
    """Upper-case a Latin-1 name the way the handler compares names.

    PFS3 folds a-z, the accented lower-case letters from 0xE0 to 0xFE and
    nothing else, leaving 0xF7 (division sign) and 0xFF alone.
    """
    return bytes(
        code - 0x20 if (0x61 <= code <= 0x7A or 0xE0 <= code <= 0xF6 or 0xF8 <= code <= 0xFE) else code
        for code in raw
    )


def encode_name(name: str, limit: int, what: str = "name") -> bytes:
    """Return the bytes PFS3 stores for a name, or explain why it cannot."""
    value = str(name or "")
    if not value or value in {".", ".."}:
        raise DataError(f"A {what} cannot be empty.")
    if any(character in value for character in ":/"):
        raise DataError(f"{value!r} contains a colon or a slash, which Amiga names cannot.")
    if any(ord(character) < 32 or 0x7F <= ord(character) < 0xA0 for character in value):
        raise DataError(f"{value!r} contains a control character.")
    try:
        raw = value.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise DataError(f"{value!r} uses characters outside the Amiga character set.") from exc
    if len(raw) > limit:
        raise DataError(f"This PFS3 volume stores {what}s of at most {limit} characters.")
    return raw


def encode_comment(comment: str | None) -> bytes:
    text = str(comment or "")
    try:
        raw = text.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise DataError("The comment uses characters outside the Amiga character set.") from exc
    if len(raw) > MAX_COMMENT:
        raise DataError(f"A comment can hold at most {MAX_COMMENT} characters.")
    if any(code < 32 for code in raw):
        raise DataError("A comment cannot contain control characters.")
    return raw


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def triple_to_datetime(days: int, mins: int, ticks: int) -> datetime:
    return datestamp_to_datetime(days, mins, ticks)


def datetime_to_triple(moment: datetime | None) -> tuple[int, int, int]:
    days, mins, ticks = datetime_to_datestamp(moment or datetime.now(timezone.utc))
    # The handler stores each part in a word.
    return min(days, 0xFFFF), mins, ticks


# ---------------------------------------------------------------------------
# Directory entries
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExtraFields:
    """The optional fields packed after a directory entry.

    With ``MODE_DIR_EXTENSION`` every entry ends in a flags word. Each set
    bit says one 16-bit field is present, and the present fields are stored
    in reverse order immediately before the flags. Only non-zero fields are
    stored, so an entry grows and shrinks as they change.
    """

    link: int = 0
    uid: int = 0
    gid: int = 0
    prot: int = 0
    virtualsize: int = 0
    rollpointer: int = 0
    fsizex: int = 0

    def words(self) -> list[int]:
        return [
            self.link >> 16, self.link & 0xFFFF, self.uid, self.gid,
            (self.prot >> 16) & 0xFFFF, self.prot & 0xFF00,
            self.virtualsize >> 16, self.virtualsize & 0xFFFF,
            self.rollpointer >> 16, self.rollpointer & 0xFFFF,
            self.fsizex,
        ]

    @classmethod
    def from_words(cls, words: list[int]) -> "ExtraFields":
        return cls(
            link=(words[0] << 16) | words[1],
            uid=words[2],
            gid=words[3],
            prot=((words[4] << 16) | words[5]) & 0xFFFFFF00,
            virtualsize=(words[6] << 16) | words[7],
            rollpointer=(words[8] << 16) | words[9],
            fsizex=words[10],
        )

    def packed(self) -> bytes:
        present = [(index, word) for index, word in enumerate(self.words()) if word]
        flags = 0
        for index, _word in present:
            flags |= 1 << index
        body = b"".join(_U16.pack(word) for _index, word in reversed(present))
        return body + _U16.pack(flags)


EXTRA_WORDS = 11


@dataclass
class DirEntry:
    """One entry of a directory block, and where it sits."""

    name: str
    raw_name: bytes
    type: int
    anode: int
    fsize: int
    days: int
    mins: int
    ticks: int
    protection: int
    comment: str
    extra: ExtraFields = field(default_factory=ExtraFields)
    size: int = 0
    block: int = 0
    offset: int = 0
    length: int = 0
    directory: int = 0

    @property
    def is_dir(self) -> bool:
        return self.type in (ST_USERDIR, ST_LINKDIR, ST_ROOT)

    @property
    def is_link(self) -> bool:
        return self.type in (ST_LINKDIR, ST_LINKFILE)

    @property
    def is_softlink(self) -> bool:
        return self.type == ST_SOFTLINK

    @property
    def full_protection(self) -> int:
        return (self.extra.prot & 0xFFFFFF00) | self.protection

    @property
    def datestamp(self) -> datetime:
        return triple_to_datetime(self.days, self.mins, self.ticks)

    @property
    def listing_anode(self) -> int:
        """The anode whose chain holds this directory's blocks."""
        if self.type == ST_LINKDIR:
            return self.extra.link
        return self.anode


def parse_entry(data, offset: int, *, dir_extension: bool, largefile: bool,
                block: int = 0, directory: int = 0) -> DirEntry:
    """Decode the entry at ``offset``, checking that it lies inside the block."""
    limit = len(data)
    length = data[offset]
    if length < ENTRY_FIXED + 2 or length & 1 or offset + length > limit:
        raise DataError(f"PFS3 directory block {block} holds a malformed entry at byte {offset}.")
    name_length = data[offset + 17]
    comment_at = offset + ENTRY_FIXED + name_length
    if comment_at >= offset + length:
        raise DataError(f"PFS3 directory block {block} holds an entry whose name overruns it.")
    comment_length = data[comment_at]
    if comment_at + 1 + comment_length > offset + length:
        raise DataError(f"PFS3 directory block {block} holds an entry whose comment overruns it.")
    raw_name = bytes(data[offset + ENTRY_FIXED : comment_at])
    entry_type = struct.unpack_from(">b", data, offset + 1)[0]
    anode, fsize = struct.unpack_from(">II", data, offset + 2)
    days, mins, ticks = struct.unpack_from(">HHH", data, offset + 10)
    extra = ExtraFields()
    if dir_extension:
        base = offset + ((DIRENTRY_SIZE + name_length + comment_length) & ~1)
        position = offset + length - 2
        if position >= base:
            flags = u16(data, position)
            words = [0] * EXTRA_WORDS
            for index in range(EXTRA_WORDS):
                if flags & (1 << index):
                    position -= 2
                    if position < base:
                        raise DataError(
                            f"PFS3 directory block {block} holds an entry whose extra fields overrun it."
                        )
                    words[index] = u16(data, position)
            extra = ExtraFields.from_words(words)
    size = fsize
    if largefile and dir_extension:
        size |= extra.fsizex << 32
    return DirEntry(
        name=raw_name.decode("latin-1"),
        raw_name=raw_name,
        type=entry_type,
        anode=anode,
        fsize=fsize,
        days=days,
        mins=mins,
        ticks=ticks,
        protection=data[offset + 16],
        comment=bytes(data[comment_at + 1 : comment_at + 1 + comment_length]).decode("latin-1"),
        extra=extra,
        size=size,
        block=block,
        offset=offset,
        length=length,
        directory=directory,
    )


def iter_block_entries(data, *, dir_extension: bool, largefile: bool, block: int = 0,
                       directory: int = 0):
    """Yield every entry of one directory block, in order."""
    offset = DIRBLOCK_HEADER
    limit = len(data)
    while offset < limit and data[offset]:
        entry = parse_entry(data, offset, dir_extension=dir_extension, largefile=largefile,
                            block=block, directory=directory)
        yield entry
        offset += entry.length


def entries_end(data) -> int:
    """The offset of the terminating zero after the last entry of a block."""
    offset = DIRBLOCK_HEADER
    limit = len(data)
    while offset < limit and data[offset]:
        offset += data[offset]
    return min(offset, limit)


def encode_entry(*, raw_name: bytes, entry_type: int, anode: int, size: int, days: int,
                 mins: int, ticks: int, protection: int, comment: bytes,
                 extra: ExtraFields, dir_extension: bool, largefile: bool) -> bytes:
    """Build a directory entry as the handler lays one out.

    The fixed part, the name and the comment are padded to an even length.
    With the directory extension the packed extra fields and their flags word
    follow, and ``next`` covers them too.
    """
    if largefile and dir_extension:
        extra = replace(extra, fsizex=(size >> 32) & 0xFFFF)
    elif size > 0xFFFFFFFF:
        raise DataError("This PFS3 volume cannot hold a file of 4 GB or more.")
    if dir_extension:
        extra = replace(extra, prot=protection & 0xFFFFFF00)
    base = (DIRENTRY_SIZE + len(raw_name) + len(comment)) & ~1
    body = bytearray(base)
    struct.pack_into(">bIIHHHBB", body, 1, entry_type, anode & 0xFFFFFFFF, size & 0xFFFFFFFF,
                     days, mins, ticks, protection & 0xFF, len(raw_name))
    body[ENTRY_FIXED : ENTRY_FIXED + len(raw_name)] = raw_name
    body[ENTRY_FIXED + len(raw_name)] = len(comment)
    body[ENTRY_FIXED + len(raw_name) + 1 : ENTRY_FIXED + len(raw_name) + 1 + len(comment)] = comment
    if dir_extension:
        body += extra.packed()
    if len(body) > MAX_ENTRY:
        raise DataError("That name and comment together are too long for a PFS3 directory entry.")
    body[0] = len(body)
    return bytes(body)


def reencode(entry: DirEntry, *, dir_extension: bool, largefile: bool, **changes) -> bytes:
    """Encode an existing entry again with some of its fields changed."""
    values = {
        "raw_name": entry.raw_name,
        "entry_type": entry.type,
        "anode": entry.anode,
        "size": entry.size,
        "days": entry.days,
        "mins": entry.mins,
        "ticks": entry.ticks,
        "protection": entry.full_protection,
        "comment": entry.comment.encode("latin-1"),
        "extra": entry.extra,
    }
    values.update(changes)
    return encode_entry(dir_extension=dir_extension, largefile=largefile, **values)


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------
def calc_num_reserved(total_sectors: int, reserved_blksize: int) -> int:
    """The reserved blocks a fresh volume gets, as the handler's format works it out."""
    taken = 32
    step = 2048
    while step and step // 2 < total_sectors:
        taken += taken * (10 if step >= 512 * 2048 else 14) // 16
        step = (step << 1) & 0xFFFFFFFF
    taken //= reserved_blksize // 1024
    taken = min(MAXNUMRESERVED, taken - 1)
    return (taken + 31) & ~0x1F


def root_cluster_blocks(num_reserved: int, reserved_blksize: int) -> int:
    """Reserved blocks the root block and the reserved bitmap occupy together.

    The first kilobyte holds the 512-byte root block and 125 longs of bitmap,
    and each further kilobyte holds 256 longs.
    """
    kilobytes = 1
    longs = 125
    while longs < num_reserved // 32:
        kilobytes += 1
        longs += 256
    return (1024 * kilobytes + reserved_blksize - 1) // reserved_blksize


__all__ = [
    "ANODE_ROOTDIR",
    "DirEntry",
    "ExtraFields",
    "PFS3_DOS_TYPES",
    "ROOT_IDS",
    "calc_num_reserved",
    "encode_entry",
    "fold",
    "iter_block_entries",
    "mode_names",
    "parse_entry",
]
