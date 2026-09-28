"""The Smart File System's on-disk structures, below the volume level.

Everything here is a pure function of block contents: identifiers, the block
checksum, the name hash, dates, the object layout inside a container and the
run encoding SFS uses in its transaction log. ``sfs`` reads volumes with these
and ``sfs_write`` changes them, so both agree on every byte by construction.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..errors import DataError
from ..file import AMIGA_EPOCH
from .blocks import ST_FILE, ST_ROOT, ST_SOFTLINK, ST_USERDIR

SFS_ID = b"SFS\x00"
OBJECTCONTAINER_ID = b"OBJC"
HASHTABLE_ID = b"HTAB"
SOFTLINK_ID = b"SLNK"
NODECONTAINER_ID = b"NDC "
BNODECONTAINER_ID = b"BNDC"
BITMAP_ID = b"BTMP"
ADMINSPACECONTAINER_ID = b"ADMC"
TRANSACTIONSTORAGE_ID = b"TRST"
TRANSACTIONFAILURE_ID = b"TRFA"
TRANSACTIONOK_ID = b"TROK"

#: The only block structure SFS 1.x writes.
STRUCTURE_VERSION = 3

ROOT_NODE = 1
RECYCLED_NODE = 2

OTYPE_HIDDEN = 1
OTYPE_UNDELETABLE = 2
OTYPE_QUICKDIR = 4
OTYPE_HARDLINK = 32
OTYPE_LINK = 64
OTYPE_DIR = 128

ROOTBITS_CASESENSITIVE = 128
ROOTBITS_RECYCLED = 64

#: The four permission bits SFS stores the other way up from AmigaDOS.
PERMISSION_BITS = 0x0F

HEADER_SIZE = 12
OBJECT_HEADER = 25
#: An object is never placed closer than this to the end of its container:
#: the size of ``struct fsObject`` as the Amiga compiler pads it, plus two.
OBJECT_TAIL = 28
CONTAINER_HEADER = HEADER_SIZE + 12
ROOT_INFO_SIZE = 36
OBJECT_NODE_SIZE = 10
EXTENT_NODE_SIZE = 14

#: Operation bits in a transaction log.
OI_EMPTY = 1
OI_DELETE = 2

#: The longest name SFS 1.x accepts, and the longest comment.
MAX_NAME_LENGTH = 100
MAX_COMMENT_LENGTH = 79

#: Metadata blocks are handed out from regions of this many blocks.
ADMIN_REGION = 32
ADMIN_HEADER = HEADER_SIZE + 12
NODE_CONTAINER_HEADER = HEADER_SIZE + 8
BNODE_CONTAINER_HEADER = HEADER_SIZE + 4
BNODE_SIZE = 8
#: An extent records its length in a 16-bit field.
MAX_EXTENT_BLOCKS = 65535
#: Free blocks SFS always keeps in hand, beyond what a change needs.
ALWAYS_FREE = 3
#: The size of an object as the Amiga compiler lays out ``struct fsObject``.
OBJECT_STRUCT_SIZE = 26

FIBF_ARCHIVE = 0x10

_LONG = struct.Struct(">I")
_WORD = struct.Struct(">H")


def long_at(data: bytes, offset: int) -> int:
    return _LONG.unpack_from(data, offset)[0]


def word_at(data: bytes, offset: int) -> int:
    return _WORD.unpack_from(data, offset)[0]


def sfs_checksum(block: bytes) -> int:
    """One plus the sum of every long in the block, modulo 2^32."""
    longs = struct.unpack(f">{len(block) // 4}I", block)
    return (1 + sum(longs)) & 0xFFFFFFFF


def put_long(block: bytearray, offset: int, value: int) -> None:
    _LONG.pack_into(block, offset, value & 0xFFFFFFFF)


def put_word(block: bytearray, offset: int, value: int) -> None:
    _WORD.pack_into(block, offset, value & 0xFFFF)


def seal_sfs_block(block: bytearray, number: int) -> bytes:
    """Stamp a block's own number and checksum, ready to be written."""
    put_long(block, 8, number)
    put_long(block, 4, 0)
    put_long(block, 4, -sfs_checksum(bytes(block)))
    return bytes(block)


def verify_sfs_block(block: bytes, number: int, block_id: bytes | None = None) -> bool:
    """Whether a block carries the expected id, a valid checksum and its own number."""
    if len(block) < HEADER_SIZE:
        return False
    if block_id is not None and block[:4] != block_id:
        return False
    return sfs_checksum(block) == 0 and long_at(block, 8) == number


def upper_char(code: int) -> int:
    """Fold one Latin-1 character to upper case the way SFS does."""
    if (224 <= code <= 254 and code != 247) or 97 <= code <= 122:
        return code - 32
    return code


def sfs_hash(name: str, case_sensitive: bool = False) -> int:
    """The 16-bit name hash SFS stores in object nodes and uses for chains.

    It is the international FFS hash, seeded with the length of the name.
    """
    raw = name.encode("latin-1")
    value = len(raw)
    for code in raw:
        value = (value * 13 + (code if case_sensitive else upper_char(code))) & 0xFFFF
    return value


def sfs_date(seconds: int) -> datetime:
    """SFS keeps dates as seconds since the start of 1978."""
    return AMIGA_EPOCH + timedelta(seconds=int(seconds))


def datetime_to_sfs(moment: datetime | None) -> int:
    """Convert a datetime into SFS seconds, treating a naive one as UTC."""
    if moment is None:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    seconds = int((moment.astimezone(timezone.utc) - AMIGA_EPOCH).total_seconds())
    if seconds < 0:
        raise DataError("SFS dates cannot precede 1 January 1978.")
    return min(seconds, 0xFFFFFFFF)


def validate_sfs_name(name: str) -> str:
    """Return a name SFS will store, or explain why it will not."""
    value = str(name or "")
    if not value or value in {".", ".."}:
        raise DataError("A name cannot be empty.")
    if any(character in value for character in ":/"):
        raise DataError(f"{value!r} contains a colon or a slash, which SFS names cannot.")
    if any(ord(character) < 32 for character in value):
        raise DataError(f"{value!r} contains a control character.")
    try:
        encoded = value.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise DataError(f"{value!r} uses characters outside the Amiga character set.") from exc
    if len(encoded) > MAX_NAME_LENGTH:
        raise DataError(f"SFS names can be at most {MAX_NAME_LENGTH} characters long.")
    return value


def compress_operation(block: bytes) -> bytes:
    """Encode one block for the transaction log, against an all-zero original.

    Runs of zero longs are stored as a single code and everything else as
    literal longs, which is how SFS itself logs a block it has just created.
    The entry is replayed onto a cleared block, so it describes the block
    completely whatever state the block on disk was left in.
    """
    longs = struct.unpack(f">{len(block) // 4}I", block)
    out = bytearray()
    index = 0
    total = len(longs)
    while index < total:
        limit = min(64, total - index)
        zeros = 0
        while zeros < limit and longs[index + zeros] == 0:
            zeros += 1
        if zeros:
            out.append(zeros - 1)
            index += zeros
            continue
        literal = 0
        while literal < limit and longs[index + literal] != 0:
            literal += 1
        out.append(0xC0 + literal - 1)
        out += struct.pack(f">{literal}I", *longs[index : index + literal])
        index += literal
    return bytes(out)


def uncompress_operation(original: bytes, data: bytes) -> bytes:
    """Rebuild one block from a transaction log entry.

    The entry is a list of runs over the block's longs: left unchanged, set to
    zero, set to all ones, or replaced by the longs that follow the code.
    """
    longs = list(struct.unpack(f">{len(original) // 4}I", original))
    position = 0
    index = 0
    while index < len(data):
        code = data[index]
        index += 1
        count = (code & 0x3F) + 1
        kind = code & 0xC0
        if position + count > len(longs):
            raise DataError("A transaction log entry runs past the end of its block.")
        if kind == 0x40:
            position += count
        elif kind == 0x80:
            longs[position : position + count] = [0xFFFFFFFF] * count
            position += count
        elif kind == 0x00:
            longs[position : position + count] = [0] * count
            position += count
        else:
            end = index + count * 4
            if end > len(data):
                raise DataError("A transaction log entry is truncated.")
            longs[position : position + count] = struct.unpack(f">{count}I", data[index:end])
            position += count
            index = end
    return struct.pack(f">{len(longs)}I", *longs)


@dataclass
class SFSObject:
    """One directory entry, decoded from its object container."""

    container: int
    offset: int
    node: int
    protection: int
    first: int
    second: int
    date: int
    bits: int
    name: str
    comment: str

    @property
    def is_dir(self) -> bool:
        return bool(self.bits & OTYPE_DIR)

    @property
    def is_softlink(self) -> bool:
        return self.bits & (OTYPE_LINK | OTYPE_HARDLINK) == OTYPE_LINK

    @property
    def hidden(self) -> bool:
        return bool(self.bits & OTYPE_HIDDEN)

    @property
    def size(self) -> int:
        return 0 if self.is_dir else self.second

    @property
    def dos_protection(self) -> int:
        return self.protection ^ PERMISSION_BITS

    @property
    def secondary_type(self) -> int:
        if self.node == ROOT_NODE:
            return ST_ROOT
        if self.is_dir:
            return ST_USERDIR
        if self.is_softlink:
            return ST_SOFTLINK
        return ST_FILE


def encode_object(
    *,
    node: int,
    protection: int,
    first: int,
    second: int,
    date: int,
    bits: int,
    name: str,
    comment: str = "",
) -> bytes:
    """Lay out one object as it is stored in a container, padded to a word."""
    raw_name = name.encode("latin-1")
    raw_comment = comment.encode("latin-1")
    data = struct.pack(">HHIIIII", 0, 0, node, protection, first, second, date)
    data += bytes([bits & 0xFF]) + raw_name + b"\0" + raw_comment + b"\0"
    if len(data) & 1:
        data += b"\0"
    return data


def object_space(name: str, comment: str = "") -> int:
    """The free bytes SFS insists on before it will place an object."""
    return OBJECT_STRUCT_SIZE + len(name.encode("latin-1")) + 2 + len(comment.encode("latin-1"))


def object_end(block: bytes) -> int:
    """Return the offset of the first unused byte in an object container."""
    offset = CONTAINER_HEADER
    limit = len(block) - OBJECT_TAIL
    while offset < limit and block[offset + OBJECT_HEADER] != 0:
        name_end = block.index(b"\0", offset + OBJECT_HEADER)
        offset = block.index(b"\0", name_end + 1) + 1
        offset += offset & 1
    return offset


def parse_objects(block: bytes, number: int) -> list[SFSObject]:
    """Decode every object stored in one object container."""
    objects: list[SFSObject] = []
    offset = CONTAINER_HEADER
    limit = len(block) - OBJECT_TAIL
    while offset < limit and block[offset + OBJECT_HEADER] != 0:
        _uid, _gid, node, protection, first, second, date = struct.unpack_from(
            ">HHIIIII", block, offset
        )
        bits = block[offset + 24]
        try:
            name_end = block.index(b"\0", offset + OBJECT_HEADER)
            comment_end = block.index(b"\0", name_end + 1)
        except ValueError as exc:
            raise DataError(f"Object container {number} holds an unterminated name.") from exc
        objects.append(
            SFSObject(
                container=number,
                offset=offset,
                node=node,
                protection=protection,
                first=first,
                second=second,
                date=date,
                bits=bits,
                name=block[offset + OBJECT_HEADER : name_end].decode("latin-1"),
                comment=block[name_end + 1 : comment_end].decode("latin-1"),
            )
        )
        offset = comment_end + 1
        offset += offset & 1
    return objects


__all__ = [
    "SFS_ID",
    "SFSObject",
    "compress_operation",
    "datetime_to_sfs",
    "encode_object",
    "object_end",
    "object_space",
    "parse_objects",
    "seal_sfs_block",
    "sfs_checksum",
    "sfs_date",
    "sfs_hash",
    "uncompress_operation",
    "upper_char",
    "validate_sfs_name",
    "verify_sfs_block",
]
