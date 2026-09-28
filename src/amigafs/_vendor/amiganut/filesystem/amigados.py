"""The AmigaDOS Old and Fast File Systems.

One class covers ``DOS\\0`` to ``DOS\\7`` because the variants differ in only
four decisions: whether data blocks carry a 24-byte header (OFS) or not
(FFS), whether name hashing folds the accented Latin-1 letters (international
mode), whether a directory keeps a cache block chain (directory cache,
``DOS\\4`` and ``DOS\\5``), and whether names may run to 107 characters
(long names, ``DOS\\6`` and ``DOS\\7``, from the FFS of AmigaOS 3.1.4 and
3.2). Each of those is a single branch rather than a separate implementation,
and keeping them together is what makes a copy between an OFS floppy and an
FFS partition an ordinary operation instead of a conversion.

The directory cache is a second copy of what each directory's header blocks
say. The Amiga's ``List`` and Workbench read the cache and never the hash
chains, so every change here that touches a header also rewrites the record
for it in its parent's cache. Reading goes through the hash chains, which are
the primary structure, and ``validate`` compares the two.

The layout of the long-name variants follows the description in amitools
(Christian Vogelgsang, GPL), which is used only as a reference for the
on-disk format; no code is taken from it. Name and comment share one area of
the header block there, and a comment that no longer fits beside a long name
moves to a comment block of its own.

Paths use AmigaDOS syntax. The volume root is an empty path or ``:``; nested
entries are separated by ``/``. Names may contain full stops, which is why the
separator is not one.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timezone

from ..errors import ConfigurationError, DataError
from ..file import (
    Access,
    AmigaMeta,
    DEFAULT_PROTECTION,
    datestamp_to_datetime,
    datetime_to_datestamp,
)
from .blocks import (
    DOS_TYPES,
    LONG_NAME_AREA,
    MAX_COMMENT,
    MAX_LONG_NAME,
    MAX_NAME,
    RESERVED_BLOCKS,
    ST_FILE,
    ST_LINKDIR,
    ST_LINKFILE,
    ST_ROOT,
    ST_SOFTLINK,
    ST_USERDIR,
    T_COMMENT,
    T_DATA,
    T_DIRCACHE,
    T_HEADER,
    T_LIST,
    WRITABLE_FORMATS,
    BlockReader,
    DirCacheRecord,
    Geometry,
    apply_checksum,
    hash_name,
    is_dircache,
    is_ffs,
    is_international,
    is_long_names,
    long_at,
    names_match,
    pack_dircache_records,
    put_long,
    put_signed_long,
    read_bstr,
    signed_long_at,
    unpack_dircache_records,
    verify_checksum,
    write_bstr,
)

OFS_DATA_HEADER = 24

# Offsets shared by root, directory and file header blocks.
OFF_TYPE = 0
OFF_HEADER_KEY = 4
OFF_HIGH_SEQ = 8
OFF_HT_SIZE = 12
OFF_FIRST_DATA = 16
OFF_CHECKSUM = 20
OFF_HASH_TABLE = 24

ILLEGAL_NAME_CHARACTERS = set(':/\\')


#: Between a bitmap held one byte per block (0 or 1) and the same bits as the
#: characters "0" and "1", which ``int`` and ``format`` convert in C.
_TO_BINARY = bytes.maketrans(b"\x00\x01", b"01")
_FROM_BINARY = bytes.maketrans(b"01", b"\x00\x01")


def _tail(block_size: int, back: int) -> int:
    """Offset of a field measured from the end of the block."""
    return block_size - back


@dataclass(frozen=True)
class Entry:
    """One catalogue entry as the workbench sees it."""

    name: str
    path: str
    is_dir: bool
    length: int
    block: int
    secondary_type: int

    @property
    def is_link(self) -> bool:
        return self.secondary_type in (ST_SOFTLINK, ST_LINKFILE, ST_LINKDIR)


@dataclass(frozen=True)
class Stat:
    """The result of ``stat`` on one path."""

    name: str
    path: str
    is_dir: bool
    length: int
    blocks: int
    block: int
    secondary_type: int


def split_path(path: str | None) -> list[str]:
    """Split an inner path into components, accepting every root spelling."""
    text = str(path or "").strip()
    if text in {"", ":", "$", "/"}:
        return []
    if text.startswith(":"):
        text = text[1:]
    elif text.startswith("$"):
        # The workbench addresses a volume root as ``$`` in legacy requests.
        text = text[1:].lstrip("/.")
    text = text.strip("/")
    if not text:
        return []
    return [part for part in text.split("/") if part not in {"", "."}]


def join_path(parts) -> str:
    return "/".join(parts)


def validate_name(name: str, limit: int = MAX_NAME) -> str:
    """Reject a name AmigaDOS could not store, before anything is written.

    ``limit`` is 30 on every volume except the long-name variants, which
    allow 107 for files and directories. A volume name stays at 30 there too.
    """
    text = str(name or "").strip()
    if not text:
        raise DataError("A name cannot be empty.")
    if len(text) > limit:
        raise DataError(f"An Amiga name can hold at most {limit} characters.")
    if any(character in ILLEGAL_NAME_CHARACTERS for character in text):
        raise DataError("An Amiga name cannot contain : / or \\.")
    if any(ord(character) < 32 for character in text):
        raise DataError("An Amiga name cannot contain control characters.")
    return text


#: The largest volume searched block by block for a root block that is not
#: where it belongs.
EXHAUSTIVE_ROOT_SEARCH_BLOCKS = 256 * 1024 * 1024 // 512


class AmigaDOSVolume:
    """A mounted OFS or FFS volume, in any of the variants ``DOS\\0`` to ``DOS\\7``."""

    def __init__(self, reader: BlockReader, geometry: Geometry | None = None):
        self.reader = reader
        self.block_size = reader.block_size
        self.total_blocks = reader.total_blocks
        self.geometry = geometry
        self.reserved = geometry.reserved if geometry else RESERVED_BLOCKS
        boot = reader.read_block(0) if self.total_blocks else b"\0" * self.block_size
        self.dos_type = boot[:4]
        if self.dos_type[:3] not in (b"DOS", b"PFS", b"SFS"):
            raise DataError(
                "No AmigaDOS boot block was found. The first four bytes are "
                f"{self.dos_type!r}, not a DOS, PFS or SFS signature."
            )
        self.format = DOS_TYPES.get(self.dos_type)
        if self.format is None:
            raise DataError(
                f"DOS type {self.dos_type[3]} is not a filing system this build recognises."
            )
        self.ffs = is_ffs(self.dos_type)
        self.international = is_international(self.dos_type)
        self.dircache = is_dircache(self.dos_type)
        self.long_names = is_long_names(self.dos_type)
        self.name_limit = MAX_LONG_NAME if self.long_names else MAX_NAME
        self.read_only = self.format not in WRITABLE_FORMATS or not reader.writable
        self.data_capacity = self.block_size if self.ffs else self.block_size - OFS_DATA_HEADER
        self.root_block = self._locate_root()
        root = self.reader.read_block(self.root_block)
        self.hash_table_size = long_at(root, OFF_HT_SIZE) or (self.block_size // 4 - 56)
        self._bitmap: bytearray | None = None
        self._bitmap_blocks: list[int] = []
        self._dirty_bitmap = False
        # Bitmap blocks whose bits have changed since they were last written.
        self._dirty_pages: set[int] = set()

    # ---- geometry ----------------------------------------------------
    def _locate_root(self) -> int:
        """Find the root block, preferring the standard mid-volume position.

        AmigaDOS puts the root block half way through the volume, counted over
        every block including the reserved boot blocks: block 880 on a
        double-density floppy and 1760 on a high-density one. The neighbours
        are tried as well because some formatters round the other way.
        """
        candidate = self.total_blocks // 2
        for block in (candidate, candidate - 1, candidate + 1):
            if 0 <= block < self.total_blocks and self._is_root(block):
                return block
        # A truncated or over-long image still mounts if a root block exists
        # anywhere sensible, which is common for hand-trimmed dumps. That is a
        # property of floppies and small hardfiles; reading every block of a
        # partition of many gigabytes to find out that it is unformatted
        # would take minutes to say what the three probes above already have.
        if self.total_blocks <= EXHAUSTIVE_ROOT_SEARCH_BLOCKS:
            for block in range(self.reserved, self.total_blocks):
                if self._is_root(block):
                    return block
        raise DataError(
            "The volume has an AmigaDOS boot block but no readable root block. "
            "It is unformatted, truncated or damaged."
        )

    def _is_root(self, block: int) -> bool:
        try:
            data = self.reader.read_block(block)
        except DataError:
            return False
        return (
            long_at(data, OFF_TYPE) == T_HEADER
            and signed_long_at(data, _tail(self.block_size, 4)) == ST_ROOT
            and verify_checksum(data)
        )

    # ---- volume identity ---------------------------------------------
    @property
    def title(self) -> str:
        root = self.reader.read_block(self.root_block)
        return read_bstr(root, _tail(self.block_size, 80), MAX_NAME)

    def set_title(self, value: str) -> None:
        name = validate_name(value)
        self._require_writable()
        root = bytearray(self.reader.read_block(self.root_block))
        write_bstr(root, _tail(self.block_size, 80), name, MAX_NAME)
        self._stamp(root, _tail(self.block_size, 92))
        self.reader.write_block(self.root_block, bytes(apply_checksum(root)))

    def volume_datestamp(self) -> datetime:
        root = self.reader.read_block(self.root_block)
        base = _tail(self.block_size, 40)
        return datestamp_to_datetime(
            long_at(root, base), long_at(root, base + 4), long_at(root, base + 8)
        )

    def created_datestamp(self) -> datetime:
        root = self.reader.read_block(self.root_block)
        base = _tail(self.block_size, 28)
        return datestamp_to_datetime(
            long_at(root, base), long_at(root, base + 4), long_at(root, base + 8)
        )

    def size_bytes(self) -> int:
        return (self.total_blocks - self.reserved) * self.data_capacity

    def free_bytes(self) -> int:
        return self._free_block_count() * self.data_capacity

    def used_bytes(self) -> int:
        return self.size_bytes() - self.free_bytes()

    # ---- bitmap ------------------------------------------------------
    def _load_bitmap(self) -> bytearray:
        if self._bitmap is not None:
            return self._bitmap
        root = self.reader.read_block(self.root_block)
        if long_at(root, _tail(self.block_size, 200)) == 0:
            raise DataError(
                "The volume's block-allocation bitmap is marked invalid. "
                "Run a validation pass before writing to it."
            )
        pages = []
        base = _tail(self.block_size, 196)
        for index in range(25):
            block = long_at(root, base + index * 4)
            if block:
                pages.append(block)
        extension = long_at(root, _tail(self.block_size, 96))
        seen = set(pages)
        while extension:
            if extension in seen or not 0 <= extension < self.total_blocks:
                raise DataError("The bitmap extension chain is damaged.")
            seen.add(extension)
            page = self.reader.read_block(extension)
            for index in range(self.block_size // 4 - 1):
                block = long_at(page, index * 4)
                if block:
                    pages.append(block)
            extension = long_at(page, self.block_size - 4)
        self._bitmap_blocks = pages
        covered = self.total_blocks - self.reserved
        # One byte per block, 1 when free. Each bitmap long holds 32 blocks
        # with the first in its lowest bit, so a long is decoded by writing it
        # out in binary and reversing it, which keeps the work in C rather
        # than in a loop over every bit of a drive of many gigabytes.
        pieces = []
        longs = self.block_size // 4 - 1
        for page_block in pages:
            page = self.reader.read_block(page_block)
            for long_index in range(longs):
                value = long_at(page, 4 + long_index * 4)
                pieces.append(format(value, "032b")[::-1])
        bits = bytearray("".join(pieces).encode("ascii").translate(_FROM_BINARY)[:covered])
        if len(bits) < covered:
            bits.extend(bytes(covered - len(bits)))
        self._bitmap = bits
        return bits

    def _page_of(self, index: int) -> int:
        return index // ((self.block_size // 4 - 1) * 32)

    def _store_bitmap(self) -> None:
        if self._bitmap is None or not self._dirty_bitmap:
            return
        bits = self._bitmap
        longs = self.block_size // 4 - 1
        per_page = longs * 32
        # Only the bitmap blocks that changed are rewritten. Rewriting every
        # one on each flush made copying many files onto a large partition
        # take longer the larger the partition was.
        pages = sorted(self._dirty_pages) if self._dirty_pages else range(len(self._bitmap_blocks))
        for page_index in pages:
            if not 0 <= page_index < len(self._bitmap_blocks):
                continue
            start = page_index * per_page
            chunk = bytes(bits[start : start + per_page]).ljust(per_page, b"\0")
            text = chunk.translate(_TO_BINARY).decode("ascii")
            page = bytearray(self.block_size)
            for long_index in range(longs):
                piece = text[long_index * 32 : long_index * 32 + 32]
                put_long(page, 4 + long_index * 4, int(piece[::-1], 2))
            put_long(page, 0, 0)
            apply_checksum(page, 0)
            self.reader.write_block(self._bitmap_blocks[page_index], bytes(page))
        self._dirty_pages.clear()
        self._dirty_bitmap = False
        if self.long_names:
            self._store_used_count()

    def _store_used_count(self) -> None:
        """Keep the long-name root block's count of blocks in use current.

        The long-name variants record the DOS type at 16 bytes from the end of
        the root block and the number of allocated blocks at 44 bytes from the
        end, both fields that the older variants leave unused.
        """
        bits = self._load_bitmap()
        root = bytearray(self.reader.read_block(self.root_block))
        used = len(bits) - bits.count(1)
        if long_at(root, _tail(self.block_size, 44)) != used:
            put_long(root, _tail(self.block_size, 44), used)
            self.reader.write_block(self.root_block, bytes(apply_checksum(root)))

    def _free_block_count(self) -> int:
        try:
            return self._load_bitmap().count(1)
        except DataError:
            return 0

    def _is_free(self, block: int) -> bool:
        bits = self._load_bitmap()
        index = block - self.reserved
        return 0 <= index < len(bits) and bool(bits[index])

    def _allocate(self, near: int | None = None) -> int:
        """Reserve one block, preferring one close to ``near``.

        Allocating outwards from the file's own header is what keeps an
        AmigaDOS volume readable at speed on real hardware, because the drive
        does not seek across the platter between consecutive data blocks.
        """
        bits = self._load_bitmap()
        start = (near if near is not None else self.root_block) - self.reserved
        start = max(0, min(start, len(bits) - 1)) if bits else 0
        # The nearest free block in either direction, the later one winning a
        # tie. Searching the bitmap rather than stepping outwards one block at
        # a time keeps a large file from taking quadratic time to allocate.
        forward = bits.find(1, start)
        backward = bits.rfind(1, 0, start)
        if forward < 0 and backward < 0:
            raise DataError("The volume is full.")
        if backward < 0 or (forward >= 0 and forward - start <= start - backward):
            candidate = forward
        else:
            candidate = backward
        bits[candidate] = 0
        self._dirty_bitmap = True
        self._dirty_pages.add(self._page_of(candidate))
        return candidate + self.reserved

    def _release(self, block: int) -> None:
        bits = self._load_bitmap()
        index = block - self.reserved
        if 0 <= index < len(bits):
            bits[index] = 1
            self._dirty_bitmap = True
            self._dirty_pages.add(self._page_of(index))

    # ---- block helpers -----------------------------------------------
    def _require_writable(self) -> None:
        if self.read_only:
            raise DataError(
                f"A {self.format} volume opened this way cannot be modified."
            )

    def _stamp(self, block: bytearray, offset: int, moment: datetime | None = None) -> None:
        days, mins, ticks = datetime_to_datestamp(moment or datetime.now(timezone.utc))
        put_long(block, offset, days)
        put_long(block, offset + 4, mins)
        put_long(block, offset + 8, ticks)

    def _read_header(self, block: int) -> bytes:
        if not 0 <= block < self.total_blocks:
            raise DataError(f"Block {block} is outside this volume.")
        data = self.reader.read_block(block)
        # A long file's block chain alternates between its header block and
        # T_LIST extension blocks; both carry the same pointer layout.
        if long_at(data, OFF_TYPE) not in (T_HEADER, T_LIST):
            raise DataError(f"Block {block} is not a header block.")
        return data

    def _entry_name(self, block: int) -> str:
        return self._header_name(self._read_header(block), block)

    # ---- header layout -----------------------------------------------
    # The long-name variants rearrange the tail of every file and directory
    # header: a 112-byte area 184 bytes from the end holds the name and then
    # the comment, each with a length byte; the pointer to an overflow comment
    # block sits 72 bytes from the end, and the date moves to 60 bytes from
    # the end. The root block keeps the classic layout on every variant.
    def _long_layout(self, block: int) -> bool:
        return self.long_names and block != self.root_block

    def _date_offset(self, block: int) -> int:
        return _tail(self.block_size, 60 if self._long_layout(block) else 92)

    def _header_name(self, header: bytes, block: int) -> str:
        if not self._long_layout(block):
            return read_bstr(header, _tail(self.block_size, 80), MAX_NAME)
        base = _tail(self.block_size, 184)
        length = min(header[base], MAX_LONG_NAME)
        return header[base + 1 : base + 1 + length].decode("latin-1")

    def _inline_comment(self, header: bytes) -> bytes:
        """Return the comment stored beside a long name, which may be empty."""
        base = _tail(self.block_size, 184)
        name_length = min(header[base], MAX_LONG_NAME)
        at = base + 1 + name_length
        length = min(header[at], LONG_NAME_AREA - 2 - name_length)
        return header[at + 1 : at + 1 + length]

    def _comment_block_of(self, header: bytes, block: int) -> int:
        """Return the overflow comment block of a long-name header, or 0.

        The pointer is honoured only when no comment is stored inline and the
        block it names really is this header's comment block, so a stray value
        can never make a delete give away a block that belongs to something
        else.
        """
        if not self._long_layout(block) or self._inline_comment(header):
            return 0
        pointer = long_at(header, _tail(self.block_size, 72))
        if not self.reserved <= pointer < self.total_blocks:
            return 0
        data = self.reader.read_block(pointer)
        if (
            long_at(data, OFF_TYPE) != T_COMMENT
            or long_at(data, 8) != block
            or not verify_checksum(data)
        ):
            return 0
        return pointer

    def _header_comment(self, header: bytes, block: int) -> str:
        if block == self.root_block:
            return ""
        if not self._long_layout(block):
            return read_bstr(header, _tail(self.block_size, 184), MAX_COMMENT)
        inline = self._inline_comment(header)
        if inline:
            return inline.decode("latin-1")
        pointer = self._comment_block_of(header, block)
        if not pointer:
            return ""
        return read_bstr(self.reader.read_block(pointer), 24, MAX_COMMENT)

    def _put_name_and_comment(
        self, header: bytearray, block: int, name: str, comment: str
    ) -> None:
        """Store a name and comment in the header being prepared for ``block``.

        On a long-name volume a comment that does not fit beside the name is
        written to a comment block of its own, allocated here near the header,
        and a comment that fits again gives that block back. The caller writes
        the header afterwards.
        """
        if not self._long_layout(block):
            write_bstr(header, _tail(self.block_size, 80), name, MAX_NAME)
            write_bstr(header, _tail(self.block_size, 184), comment, MAX_COMMENT)
            return
        encoded_name = name.encode("latin-1", "replace")[:MAX_LONG_NAME]
        encoded_comment = comment.encode("latin-1", "replace")[:MAX_COMMENT]
        pointer = self._comment_block_of(bytes(header), block)
        area = bytearray(LONG_NAME_AREA)
        area[0] = len(encoded_name)
        area[1 : 1 + len(encoded_name)] = encoded_name
        if 2 + len(encoded_name) + len(encoded_comment) <= LONG_NAME_AREA:
            at = 1 + len(encoded_name)
            area[at] = len(encoded_comment)
            area[at + 1 : at + 1 + len(encoded_comment)] = encoded_comment
            if pointer:
                self._release(pointer)
            pointer = 0
        else:
            if not pointer:
                pointer = self._allocate(block)
            overflow = bytearray(self.block_size)
            put_long(overflow, OFF_TYPE, T_COMMENT)
            put_long(overflow, OFF_HEADER_KEY, pointer)
            put_long(overflow, 8, block)
            write_bstr(overflow, 24, comment, MAX_COMMENT)
            self.reader.write_block(pointer, bytes(apply_checksum(overflow)))
        base = _tail(self.block_size, 184)
        header[base : base + LONG_NAME_AREA] = area
        put_long(header, _tail(self.block_size, 72), pointer)

    # ---- directory cache ---------------------------------------------
    # Each directory on a DOS\4 or DOS\5 volume, the root included, points
    # from the long 8 bytes before the end of its header to a chain of cache
    # blocks. A cache block carries its own number, the directory it belongs
    # to, a record count and the next block of the chain, then the records.
    def _cache_record(self, block: int, header: bytes | None = None) -> DirCacheRecord:
        """Build the cache record that describes the header at ``block``."""
        if header is None:
            header = self._read_header(block)
        secondary = signed_long_at(header, _tail(self.block_size, 4))
        base = self._date_offset(block)
        uid, gid = struct.unpack_from(">HH", header, _tail(self.block_size, 196))
        directory = secondary in (ST_ROOT, ST_USERDIR, ST_LINKDIR)
        return DirCacheRecord(
            header=block,
            size=0 if directory else long_at(header, _tail(self.block_size, 188)),
            protection=long_at(header, _tail(self.block_size, 192)),
            uid=uid,
            gid=gid,
            days=long_at(header, base),
            mins=long_at(header, base + 4),
            ticks=long_at(header, base + 8),
            secondary_type=((secondary + 128) & 0xFF) - 128,
            name=self._header_name(header, block).encode("latin-1"),
            comment=self._header_comment(header, block).encode("latin-1"),
        )

    def _load_cache(self, directory: int) -> list[list]:
        """Return a directory's cache chain as ``[block, records, raw]`` lists."""
        header = self._read_header(directory)
        chain: list[list] = []
        seen: set[int] = set()
        current = long_at(header, _tail(self.block_size, 8))
        while current:
            if current in seen or not self.reserved <= current < self.total_blocks:
                raise DataError("A directory-cache chain is damaged.")
            seen.add(current)
            raw = self.reader.read_block(current)
            if long_at(raw, OFF_TYPE) != T_DIRCACHE or not verify_checksum(raw):
                raise DataError(f"Block {current} is not a valid directory-cache block.")
            records = unpack_dircache_records(raw, long_at(raw, 12))
            chain.append([current, records, raw])
            current = long_at(raw, 16)
        return chain

    def _cache_capacity(self) -> int:
        return self.block_size - 24

    def _edit_cache(
        self,
        directory: int,
        *,
        drop: int | None = None,
        record: DirCacheRecord | None = None,
    ) -> None:
        """Remove, replace or add one record in a directory's cache.

        ``drop`` removes the record for that header block. ``record`` replaces
        the record with the same header block, in place when it still fits,
        and otherwise goes into the first block with room, a new block being
        added to the end of the chain when none has any. A block left empty is
        released unless it is the only one, because every directory keeps at
        least one. Only the blocks whose contents changed are written.
        """
        if not self.dircache:
            return
        chain = self._load_cache(directory)
        capacity = self._cache_capacity()

        def used(records) -> int:
            return sum(item.packed_size for item in records)

        key = record.header if record is not None else drop
        pending = record
        if key is not None:
            for entry in chain:
                records = entry[1]
                for index, existing in enumerate(records):
                    if existing.header != key:
                        continue
                    if pending is not None and (
                        used(records) - existing.packed_size + pending.packed_size
                        <= capacity
                    ):
                        records[index] = pending
                        pending = None
                    else:
                        del records[index]
                    break
                else:
                    continue
                break
        if pending is not None:
            for entry in chain:
                if used(entry[1]) + pending.packed_size <= capacity:
                    entry[1].append(pending)
                    break
            else:
                near = chain[-1][0] if chain else directory
                chain.append([self._allocate(near), [pending], None])
        if not chain:
            chain.append([self._allocate(directory), [], None])
        for entry in list(chain):
            if not entry[1] and len(chain) > 1:
                chain.remove(entry)
                self._release(entry[0])
        self._commit_cache(directory, chain)

    def _commit_cache(self, directory: int, chain: list[list]) -> None:
        """Write a directory's cache chain and point its header at the first block."""
        for position in range(len(chain) - 1, -1, -1):
            block, records, original = chain[position]
            following = chain[position + 1][0] if position + 1 < len(chain) else 0
            raw = bytearray(self.block_size)
            put_long(raw, OFF_TYPE, T_DIRCACHE)
            put_long(raw, OFF_HEADER_KEY, block)
            put_long(raw, 8, directory)
            put_long(raw, 12, len(records))
            put_long(raw, 16, following)
            raw[24:] = pack_dircache_records(records, self.block_size)
            apply_checksum(raw)
            if original != bytes(raw):
                self.reader.write_block(block, bytes(raw))
        header = bytearray(self._read_header(directory))
        first = chain[0][0] if chain else 0
        if long_at(header, _tail(self.block_size, 8)) != first:
            put_long(header, _tail(self.block_size, 8), first)
            self.reader.write_block(directory, bytes(apply_checksum(header)))

    def _sync_cache_record(self, block: int) -> None:
        """Bring the record for ``block`` in its parent's cache up to date."""
        if not self.dircache or block == self.root_block:
            return
        header = self._read_header(block)
        parent = long_at(header, _tail(self.block_size, 12))
        self._edit_cache(parent, record=self._cache_record(block, header))

    def rebuild_dircache(self) -> int:
        """Rewrite every directory's cache from its hash chains.

        This repairs a ``DOS\\4`` or ``DOS\\5`` volume whose caches went stale,
        for example because an older tool changed it without maintaining
        them. Existing cache blocks are reused where the chain is readable;
        a chain that is damaged is abandoned, and the blocks it held are left
        for a validation pass rather than freed on a guess. Returns the number
        of directories whose cache was written.
        """
        self._require_writable()
        if not self.dircache:
            return 0
        capacity = self._cache_capacity()
        rewritten = 0
        pending = [self.root_block]
        while pending:
            directory = pending.pop()
            records = []
            for child in self._chain_blocks(directory):
                header = self._read_header(child)
                records.append(self._cache_record(child, header))
                if signed_long_at(header, _tail(self.block_size, 4)) == ST_USERDIR:
                    pending.append(child)
            try:
                old = self._load_cache(directory)
            except DataError:
                old = []
            chain: list[list] = []
            spare = [(entry[0], entry[2]) for entry in old]
            current: list[DirCacheRecord] = []
            groups: list[list[DirCacheRecord]] = []
            for record in records:
                if sum(item.packed_size for item in current) + record.packed_size > capacity:
                    groups.append(current)
                    current = []
                current.append(record)
            groups.append(current)
            for group in groups:
                if spare:
                    block, raw = spare.pop(0)
                else:
                    near = chain[-1][0] if chain else directory
                    block, raw = self._allocate(near), None
                chain.append([block, group, raw])
            for block, _raw in spare:
                self._release(block)
            self._commit_cache(directory, chain)
            rewritten += 1
        self._store_bitmap()
        return rewritten

    def _secondary_type(self, block: int) -> int:
        return signed_long_at(self._read_header(block), _tail(self.block_size, 4))

    def _hash_table(self, block: int) -> list[int]:
        data = self._read_header(block)
        return [
            long_at(data, OFF_HASH_TABLE + index * 4)
            for index in range(self.hash_table_size)
        ]

    # ---- lookup ------------------------------------------------------
    def _find_in_directory(self, directory_block: int, name: str) -> int | None:
        slot = hash_name(name, self.international, self.hash_table_size)
        data = self._read_header(directory_block)
        candidate = long_at(data, OFF_HASH_TABLE + slot * 4)
        seen = set()
        while candidate:
            if candidate in seen or not 0 <= candidate < self.total_blocks:
                raise DataError("A directory hash chain is damaged.")
            seen.add(candidate)
            if names_match(self._entry_name(candidate), name, self.international):
                return candidate
            candidate = long_at(
                self._read_header(candidate), _tail(self.block_size, 16)
            )
        return None

    def _resolve(self, path: str | None) -> tuple[int, list[str]]:
        parts = split_path(path)
        block = self.root_block
        for index, part in enumerate(parts):
            found = self._find_in_directory(block, part)
            if found is None:
                raise DataError(f"Path not found: {join_path(parts[: index + 1])}")
            block = found
            if index < len(parts) - 1 and self._secondary_type(block) not in (
                ST_USERDIR,
                ST_ROOT,
                ST_LINKDIR,
            ):
                raise DataError(f"{join_path(parts[: index + 1])} is not a directory.")
        return block, parts

    def exists(self, path: str | None) -> bool:
        try:
            self._resolve(path)
        except DataError:
            return False
        return True

    def stat(self, path: str | None) -> Stat:
        block, parts = self._resolve(path)
        secondary = ST_ROOT if block == self.root_block else self._secondary_type(block)
        is_dir = secondary in (ST_ROOT, ST_USERDIR, ST_LINKDIR)
        length = 0 if is_dir else long_at(self._read_header(block), _tail(self.block_size, 188))
        return Stat(
            name=parts[-1] if parts else self.title,
            path=join_path(parts),
            is_dir=is_dir,
            length=length,
            blocks=self._entry_block_count(block, is_dir),
            block=block,
            secondary_type=secondary,
        )

    def _entry_block_count(self, block: int, is_dir: bool) -> int:
        if is_dir:
            return 1
        size = long_at(self._read_header(block), _tail(self.block_size, 188))
        if size <= 0:
            return 1
        data_blocks = (size + self.data_capacity - 1) // self.data_capacity
        extensions = max(0, (data_blocks - 1) // self.hash_table_size)
        return 1 + data_blocks + extensions

    # ---- listing -----------------------------------------------------
    def iter_entries(self, path: str | None = None):
        block, parts = self._resolve(path)
        secondary = ST_ROOT if block == self.root_block else self._secondary_type(block)
        if secondary not in (ST_ROOT, ST_USERDIR, ST_LINKDIR):
            raise DataError(f"{join_path(parts)} is not a directory.")
        prefix = join_path(parts)
        for candidate in self._chain_blocks(block):
            child = self._read_header(candidate)
            name = self._header_name(child, candidate)
            child_secondary = signed_long_at(child, _tail(self.block_size, 4))
            is_dir = child_secondary in (ST_USERDIR, ST_LINKDIR)
            yield Entry(
                name=name,
                path=f"{prefix}/{name}" if prefix else name,
                is_dir=is_dir,
                length=0 if is_dir else long_at(child, _tail(self.block_size, 188)),
                block=candidate,
                secondary_type=child_secondary,
            )

    def _chain_blocks(self, directory_block: int) -> list[int]:
        blocks: list[int] = []
        seen: set[int] = set()
        for candidate in self._hash_table(directory_block):
            while candidate:
                if candidate in seen or not 0 <= candidate < self.total_blocks:
                    raise DataError("A directory hash chain is damaged.")
                seen.add(candidate)
                blocks.append(candidate)
                candidate = long_at(
                    self._read_header(candidate), _tail(self.block_size, 16)
                )
        return blocks

    # ---- reading -----------------------------------------------------
    def read_bytes(self, path: str) -> bytes:
        block, parts = self._resolve(path)
        header = self._read_header(block)
        if signed_long_at(header, _tail(self.block_size, 4)) not in (ST_FILE, ST_LINKFILE):
            raise DataError(f"{join_path(parts)} is not a file.")
        size = long_at(header, _tail(self.block_size, 188))
        chunks: list[bytes] = []
        remaining = size
        for data_block in self._data_blocks(block):
            if remaining <= 0:
                break
            raw = self.reader.read_block(data_block)
            payload = raw[OFS_DATA_HEADER:] if not self.ffs else raw
            if not self.ffs:
                used = long_at(raw, 12)
                payload = payload[: max(0, min(used, len(payload)))]
            chunks.append(payload[:remaining])
            remaining -= len(chunks[-1])
        data = b"".join(chunks)
        if len(data) < size:
            raise DataError(
                f"{join_path(parts)} declares {size:,} bytes but only "
                f"{len(data):,} are present. The file is truncated."
            )
        return data[:size]

    def _data_blocks(self, header_block: int) -> list[int]:
        blocks: list[int] = []
        current = header_block
        seen = {header_block}
        while current:
            header = self._read_header(current)
            count = long_at(header, OFF_HIGH_SEQ)
            for index in range(count):
                offset = OFF_HASH_TABLE + (self.hash_table_size - 1 - index) * 4
                block = long_at(header, offset)
                if block:
                    blocks.append(block)
            current = long_at(header, _tail(self.block_size, 8))
            if current:
                if current in seen or not 0 <= current < self.total_blocks:
                    raise DataError("A file extension chain is damaged.")
                seen.add(current)
        return blocks

    # ---- metadata ----------------------------------------------------
    def amiga_meta(self, path: str) -> AmigaMeta:
        block, _parts = self._resolve(path)
        header = self._read_header(block)
        base = self._date_offset(block)
        stamp = datestamp_to_datetime(
            long_at(header, base), long_at(header, base + 4), long_at(header, base + 8)
        )
        if block == self.root_block:
            # The root block keeps bitmap pointers where an entry keeps its
            # protection and comment, so it has neither to report.
            return AmigaMeta(protection=DEFAULT_PROTECTION, comment="", datestamp=stamp)
        protection = long_at(header, _tail(self.block_size, 192))
        comment = self._header_comment(header, block)
        return AmigaMeta(protection=protection, comment=comment, datestamp=stamp)

    def set_amiga_meta(self, path: str, meta: AmigaMeta) -> None:
        self._require_writable()
        block, _parts = self._resolve(path)
        header = bytearray(self._read_header(block))
        if block == self.root_block:
            # Only the date applies to the root: the offsets that hold an
            # entry's protection and comment hold bitmap pointers here.
            if meta.datestamp is not None:
                self._stamp(header, self._date_offset(block), meta.datestamp)
                self.reader.write_block(block, bytes(apply_checksum(header)))
            return
        comment = str(meta.comment or "")
        if len(comment) > MAX_COMMENT:
            raise DataError(f"A comment can hold at most {MAX_COMMENT} characters.")
        put_long(header, _tail(self.block_size, 192), int(meta.protection) & 0xFFFFFFFF)
        self._put_name_and_comment(header, block, self._header_name(header, block), comment)
        if meta.datestamp is not None:
            self._stamp(header, self._date_offset(block), meta.datestamp)
        self.reader.write_block(block, bytes(apply_checksum(header)))
        self._sync_cache_record(block)
        self._store_bitmap()

    def access(self, path: str) -> Access:
        return self.amiga_meta(path).access

    def set_access(self, path: str, access: Access | int) -> None:
        value = access.value if isinstance(access, Access) else int(access)
        meta = self.amiga_meta(path)
        self.set_amiga_meta(path, meta.with_protection(value))

    def comment(self, path: str) -> str:
        return self.amiga_meta(path).comment

    def set_comment(self, path: str, value: str) -> None:
        self.set_amiga_meta(path, self.amiga_meta(path).with_comment(value))

    def datestamp(self, path: str) -> datetime:
        return self.amiga_meta(path).datestamp

    def set_datestamp(self, path: str, moment: datetime) -> None:
        self._require_writable()
        block, _parts = self._resolve(path)
        header = bytearray(self._read_header(block))
        self._stamp(header, self._date_offset(block), moment)
        self.reader.write_block(block, bytes(apply_checksum(header)))
        self._sync_cache_record(block)
        self._store_bitmap()

    # ---- writing -----------------------------------------------------
    def mkdir(self, path: str) -> int:
        self._require_writable()
        parts = split_path(path)
        if not parts:
            raise DataError("The volume root already exists.")
        name = validate_name(parts[-1], self.name_limit)
        parent_block, _ = self._resolve(join_path(parts[:-1]))
        if self._find_in_directory(parent_block, name) is not None:
            raise DataError(f"{path} already exists.")
        block = self._allocate(parent_block)
        header = bytearray(self.block_size)
        put_long(header, OFF_TYPE, T_HEADER)
        put_long(header, OFF_HEADER_KEY, block)
        put_long(header, _tail(self.block_size, 192), DEFAULT_PROTECTION)
        self._put_name_and_comment(header, block, name, "")
        self._stamp(header, self._date_offset(block))
        put_long(header, _tail(self.block_size, 12), parent_block)
        put_signed_long(header, _tail(self.block_size, 4), ST_USERDIR)
        self.reader.write_block(block, bytes(apply_checksum(header)))
        # A new directory starts with one empty cache block of its own.
        self._edit_cache(block)
        self._link_into(parent_block, block, name)
        self._store_bitmap()
        return block

    def write_bytes(self, path: str, data: bytes, meta: AmigaMeta | None = None) -> int:
        """Create or replace a file, allocating its data blocks in order."""
        self._require_writable()
        parts = split_path(path)
        if not parts:
            raise DataError("A file needs a name.")
        name = validate_name(parts[-1], self.name_limit)
        source_comment = str((meta.comment if meta else "") or "")
        if len(source_comment) > MAX_COMMENT:
            raise DataError(f"A comment can hold at most {MAX_COMMENT} characters.")
        parent_block, _ = self._resolve(join_path(parts[:-1]))
        existing = self._find_in_directory(parent_block, name)
        preserved = None
        if existing is not None:
            preserved = self.amiga_meta(join_path(parts))
            self.remove(join_path(parts))
        payload = bytes(data)
        needed = (len(payload) + self.data_capacity - 1) // self.data_capacity
        extensions = max(0, (needed - 1) // self.hash_table_size)
        # One more block may be needed for the parent's cache on a directory
        # cache volume, or for an overflow comment on a long-name volume.
        spare = 1 if self.dircache or self.long_names else 0
        if needed + extensions + 1 + spare > self._free_block_count():
            raise DataError(
                f"{len(payload):,} bytes need {needed + extensions + 1 + spare:,} blocks "
                f"but only {self._free_block_count():,} are free."
            )
        header_block = self._allocate(parent_block)
        data_blocks = [self._allocate(header_block) for _ in range(needed)]
        extension_blocks = [self._allocate(header_block) for _ in range(extensions)]

        # Data blocks first, so a failure never leaves a header pointing at
        # blocks that were never written.
        for index, block in enumerate(data_blocks):
            chunk = payload[index * self.data_capacity : (index + 1) * self.data_capacity]
            if self.ffs:
                self.reader.write_block(block, chunk.ljust(self.block_size, b"\0"))
                continue
            raw = bytearray(self.block_size)
            put_long(raw, 0, T_DATA)
            put_long(raw, 4, header_block)
            put_long(raw, 8, index + 1)
            put_long(raw, 12, len(chunk))
            put_long(raw, 16, data_blocks[index + 1] if index + 1 < needed else 0)
            raw[OFS_DATA_HEADER : OFS_DATA_HEADER + len(chunk)] = chunk
            self.reader.write_block(block, bytes(apply_checksum(raw)))

        chain = [header_block, *extension_blocks]
        for position, block in enumerate(chain):
            first = position * self.hash_table_size
            slice_blocks = data_blocks[first : first + self.hash_table_size]
            raw = bytearray(self.block_size)
            put_long(raw, OFF_TYPE, T_HEADER if position == 0 else T_LIST)
            put_long(raw, OFF_HEADER_KEY, block)
            put_long(raw, OFF_HIGH_SEQ, len(slice_blocks))
            put_long(raw, OFF_FIRST_DATA, slice_blocks[0] if slice_blocks else 0)
            for index, data_block in enumerate(slice_blocks):
                put_long(
                    raw,
                    OFF_HASH_TABLE + (self.hash_table_size - 1 - index) * 4,
                    data_block,
                )
            if position == 0:
                source = meta or preserved
                put_long(
                    raw,
                    _tail(self.block_size, 192),
                    int(source.protection) & 0xFFFFFFFF if source else DEFAULT_PROTECTION,
                )
                put_long(raw, _tail(self.block_size, 188), len(payload))
                comment = str((source.comment if source else "") or "")[:MAX_COMMENT]
                self._put_name_and_comment(raw, block, name, comment)
                self._stamp(
                    raw,
                    self._date_offset(block),
                    source.datestamp if source and source.datestamp else None,
                )
                put_long(raw, _tail(self.block_size, 12), parent_block)
            else:
                put_long(raw, _tail(self.block_size, 12), header_block)
            put_long(
                raw,
                _tail(self.block_size, 8),
                chain[position + 1] if position + 1 < len(chain) else 0,
            )
            put_signed_long(raw, _tail(self.block_size, 4), ST_FILE)
            self.reader.write_block(block, bytes(apply_checksum(raw)))

        self._link_into(parent_block, header_block, name)
        self._store_bitmap()
        return header_block

    def _link_into(self, parent_block: int, child_block: int, name: str) -> None:
        slot = hash_name(name, self.international, self.hash_table_size)
        parent = bytearray(self._read_header(parent_block))
        head = long_at(parent, OFF_HASH_TABLE + slot * 4)
        child = bytearray(self._read_header(child_block))
        put_long(child, _tail(self.block_size, 16), head)
        self.reader.write_block(child_block, bytes(apply_checksum(child)))
        put_long(parent, OFF_HASH_TABLE + slot * 4, child_block)
        self._stamp(parent, self._date_offset(parent_block))
        self.reader.write_block(parent_block, bytes(apply_checksum(parent)))
        self._edit_cache(parent_block, record=self._cache_record(child_block))
        # The parent's date changed, and its own parent caches that date.
        self._sync_cache_record(parent_block)

    def _unlink(self, parent_block: int, child_block: int, name: str) -> None:
        slot = hash_name(name, self.international, self.hash_table_size)
        parent = bytearray(self._read_header(parent_block))
        head = long_at(parent, OFF_HASH_TABLE + slot * 4)
        successor = long_at(self._read_header(child_block), _tail(self.block_size, 16))
        if head == child_block:
            put_long(parent, OFF_HASH_TABLE + slot * 4, successor)
        else:
            previous = head
            seen = set()
            while True:
                if not previous or previous in seen:
                    raise DataError(f"{name} is not linked into its parent directory.")
                seen.add(previous)
                block = bytearray(self._read_header(previous))
                following = long_at(block, _tail(self.block_size, 16))
                if following == child_block:
                    put_long(block, _tail(self.block_size, 16), successor)
                    self.reader.write_block(previous, bytes(apply_checksum(block)))
                    break
                previous = following
        self._stamp(parent, self._date_offset(parent_block))
        self.reader.write_block(parent_block, bytes(apply_checksum(parent)))
        self._edit_cache(parent_block, drop=child_block)
        self._sync_cache_record(parent_block)

    def remove(self, path: str, *, recursive: bool = False) -> None:
        self._require_writable()
        parts = split_path(path)
        if not parts:
            raise DataError("The volume root cannot be deleted.")
        block, _ = self._resolve(path)
        parent_block, _ = self._resolve(join_path(parts[:-1]))
        secondary = self._secondary_type(block)
        if secondary in (ST_USERDIR, ST_LINKDIR):
            children = list(self.iter_entries(path))
            if children and not recursive:
                raise DataError(f"{path} is not empty.")
            for child in children:
                self.remove(child.path, recursive=True)
            if self.dircache:
                try:
                    cache = self._load_cache(block)
                except DataError:
                    # A damaged chain is left allocated for validation to
                    # report, rather than freeing blocks it may not own.
                    cache = []
                for entry in cache:
                    self._release(entry[0])
        else:
            if self.access(path).locked:
                raise DataError(f"{path} is protected against deletion.")
            for data_block in self._data_blocks(block):
                self._release(data_block)
            current = long_at(self._read_header(block), _tail(self.block_size, 8))
            while current:
                following = long_at(self._read_header(current), _tail(self.block_size, 8))
                self._release(current)
                current = following
        header = self._read_header(block)
        comment_block = self._comment_block_of(header, block)
        self._unlink(parent_block, block, self._header_name(header, block))
        if comment_block:
            self._release(comment_block)
        self._release(block)
        self._store_bitmap()

    def rename(self, source: str, destination: str) -> None:
        self._require_writable()
        source_parts = split_path(source)
        destination_parts = split_path(destination)
        if not source_parts or not destination_parts:
            raise DataError("Both a source and a destination name are required.")
        name = validate_name(destination_parts[-1], self.name_limit)
        block, _ = self._resolve(source)
        old_parent, _ = self._resolve(join_path(source_parts[:-1]))
        new_parent, _ = self._resolve(join_path(destination_parts[:-1]))
        clash = self._find_in_directory(new_parent, name)
        if clash is not None and clash != block:
            raise DataError(f"{destination} already exists.")
        self._unlink(old_parent, block, self._entry_name(block))
        header = bytearray(self._read_header(block))
        # A longer name can push a long-name volume's comment out to a block
        # of its own, and a shorter one can bring it back.
        comment = self._header_comment(header, block)
        self._put_name_and_comment(header, block, name, comment)
        put_long(header, _tail(self.block_size, 12), new_parent)
        put_long(header, _tail(self.block_size, 16), 0)
        self.reader.write_block(block, bytes(apply_checksum(header)))
        self._link_into(new_parent, block, name)
        self._store_bitmap()

    # ---- boot block --------------------------------------------------
    def boot_option(self) -> int:
        """Return 1 when the volume carries executable boot code, else 0."""
        boot = self.reader.read_block(0) + self.reader.read_block(1)
        return 1 if any(boot[12:]) else 0

    def set_boot_option(self, option: int) -> None:
        """Write or clear a standard AmigaDOS boot block."""
        self._require_writable()
        option = int(option)
        if option not in (0, 1):
            raise ConfigurationError("A boot option is either 0 (off) or 1 (bootable).")
        first = bytearray(self.block_size)
        second = bytearray(self.block_size)
        first[0:4] = self.dos_type
        if option:
            first[12 : 12 + len(STANDARD_BOOT_CODE)] = STANDARD_BOOT_CODE
        checksum = _boot_checksum(bytes(first) + bytes(second))
        struct.pack_into(">I", first, 4, checksum)
        self.reader.write_block(0, bytes(first))
        self.reader.write_block(1, bytes(second))

    # ---- maintenance -------------------------------------------------
    def validate(self) -> list[str]:
        """Walk every structure and report what a real machine would refuse."""
        problems: list[str] = []
        root = self.reader.read_block(self.root_block)
        if not verify_checksum(root):
            problems.append("The root block checksum is wrong.")
        if long_at(root, _tail(self.block_size, 200)) == 0:
            problems.append("The block-allocation bitmap is marked invalid.")
        allocated: dict[int, str] = {}

        def claim(block: int, owner: str) -> None:
            if block in allocated:
                problems.append(
                    f"Block {block} is claimed by both {allocated[block]} and {owner}."
                )
            allocated[block] = owner

        def walk(directory: str, directory_block: int) -> None:
            if self.dircache:
                self._check_cache(directory_block, directory or "the root", problems, claim)
            for entry in self.iter_entries(directory):
                header = self.reader.read_block(entry.block)
                if not verify_checksum(header):
                    problems.append(f"{entry.path} has a bad header checksum.")
                claim(entry.block, entry.path)
                if self._long_layout(entry.block) and not self._inline_comment(header):
                    pointer = long_at(header, _tail(self.block_size, 72))
                    if pointer and self._comment_block_of(header, entry.block) != pointer:
                        problems.append(f"{entry.path} points to a damaged comment block.")
                    elif pointer:
                        claim(pointer, f"the comment of {entry.path}")
                if entry.is_dir:
                    walk(entry.path, entry.block)
                    continue
                if entry.is_link:
                    continue
                try:
                    blocks = self._data_blocks(entry.block)
                except DataError as error:
                    problems.append(f"{entry.path}: {error}")
                    continue
                expected = (
                    entry.length + self.data_capacity - 1
                ) // self.data_capacity
                if len(blocks) < expected:
                    problems.append(
                        f"{entry.path} is missing {expected - len(blocks)} data block(s)."
                    )
                for block in blocks:
                    claim(block, entry.path)

        try:
            walk("", self.root_block)
        except DataError as error:
            problems.append(str(error))

        try:
            bits = self._load_bitmap()
        except DataError as error:
            problems.append(str(error))
            return problems
        for block, owner in allocated.items():
            index = block - self.reserved
            if 0 <= index < len(bits) and bits[index]:
                problems.append(
                    f"Block {block} is used by {owner} but the bitmap marks it free."
                )
        if self.long_names and long_at(root, _tail(self.block_size, 16)) == int.from_bytes(
            self.dos_type, "big"
        ):
            used = len(bits) - bits.count(1)
            recorded = long_at(root, _tail(self.block_size, 44))
            if recorded != used:
                problems.append(
                    f"The root block counts {recorded} blocks in use but the bitmap has {used}."
                )
        return problems

    def _check_cache(self, directory: int, label: str, problems: list[str], claim) -> None:
        """Compare one directory's cache with the headers its hash chains reach."""
        try:
            chain = self._load_cache(directory)
        except DataError as error:
            problems.append(f"The directory cache of {label}: {error}")
            return
        if not chain:
            problems.append(f"No directory cache was found for {label}.")
            return
        cached: dict[int, DirCacheRecord] = {}
        for block, records, raw in chain:
            claim(block, f"the directory cache of {label}")
            if long_at(raw, OFF_HEADER_KEY) != block or long_at(raw, 8) != directory:
                problems.append(
                    f"Cache block {block} of {label} does not name itself and its directory."
                )
            for record in records:
                if record.header in cached:
                    problems.append(f"The cache of {label} lists block {record.header} twice.")
                cached[record.header] = record
        for child in self._chain_blocks(directory):
            expected = self._cache_record(child)
            found = cached.pop(child, None)
            name = expected.name.decode("latin-1")
            if found is None:
                problems.append(f"The cache of {label} has no record for {name}.")
            elif found != expected:
                fields = [
                    field
                    for field in (
                        "size", "protection", "uid", "gid", "days", "mins", "ticks",
                        "secondary_type", "name", "comment",
                    )
                    if getattr(found, field) != getattr(expected, field)
                ]
                problems.append(
                    f"The cache of {label} disagrees with the header of {name} "
                    f"({', '.join(fields)})."
                )
        for record in cached.values():
            problems.append(
                f"The cache of {label} lists {record.name.decode('latin-1')}, "
                "which is not in the directory."
            )

    def defragment(self) -> int:
        """Rewrite every file so its data blocks are contiguous again.

        Returns the number of files that moved. The catalogue is walked
        depth-first and each file is rewritten in place, which is safe because
        the block is released before the replacement is allocated.
        """
        self._require_writable()
        moved = 0
        paths: list[str] = []

        def collect(directory: str) -> None:
            for entry in self.iter_entries(directory):
                if entry.is_dir:
                    collect(entry.path)
                elif not entry.is_link:
                    paths.append(entry.path)

        collect("")
        for path in paths:
            meta = self.amiga_meta(path)
            data = self.read_bytes(path)
            blocks_before = self._data_blocks(self._resolve(path)[0])
            contiguous = all(
                blocks_before[index + 1] == blocks_before[index] + 1
                for index in range(len(blocks_before) - 1)
            )
            if contiguous:
                continue
            self.write_bytes(path, data, meta)
            moved += 1
        self._store_bitmap()
        return moved

    def free_map(self) -> list[bool]:
        """Return one flag per addressable block: True when it is free."""
        bits = self._load_bitmap()
        return [False] * self.reserved + [bool(value) for value in bits]

    def flush(self) -> None:
        self._store_bitmap()
        self.reader.flush()

    def close(self) -> None:
        """Flush pending bitmap changes, then release the file handle."""
        try:
            self.flush()
        finally:
            self.reader.close()


def _boot_checksum(boot: bytes) -> int:
    total = 0
    for index in range(0, len(boot), 4):
        if index == 4:
            continue
        (value,) = struct.unpack_from(">I", boot, index)
        total += value
        if total > 0xFFFFFFFF:
            total = (total + 1) & 0xFFFFFFFF
    return (~total) & 0xFFFFFFFF


# The 68000 boot code Commodore shipped: open dos.library and return its base
# so the ROM continues the boot. Anything shorter is not accepted by 1.3.
STANDARD_BOOT_CODE = bytes.fromhex(
    "43fa003e 4eaeffa0 4a80670a 2040207a 00204e75"
    "70004e75 646f732e 6c696272 61727900".replace(" ", "")
)


#: A volume up to this many blocks is cleared from end to end when it is
#: formatted, as a floppy always has been. A larger one has only the blocks
#: that describe it written, which is what keeps a new drive of many
#: gigabytes a sparse file and formatting it a matter of seconds.
WIPE_LIMIT_BLOCKS = 64 * 1024 * 1024 // 512

#: How much of the start of a large volume is cleared, so that nothing left
#: from an earlier filing system is found there by a program that looks.
WIPE_HEAD_BYTES = 64 * 1024

#: The root block holds this many bitmap block numbers. The rest go in
#: bitmap extension blocks chained from it.
ROOT_BITMAP_PAGES = 25


def _packed_bitmap_page(bits: bytes, block_size: int) -> bytes:
    """Pack one byte per block, 1 when free, into a checksummed bitmap block."""
    longs = block_size // 4 - 1
    text = bits.ljust(longs * 32, b"\0").translate(_TO_BINARY).decode("ascii")
    page = bytearray(block_size)
    for index in range(longs):
        piece = text[index * 32 : index * 32 + 32]
        put_long(page, 4 + index * 4, int(piece[::-1], 2))
    apply_checksum(page, 0)
    return bytes(page)


def format_volume(
    reader: BlockReader,
    *,
    label: str = "Empty",
    dos_type: bytes = b"DOS\x00",
    bootable: bool = False,
    geometry: Geometry | None = None,
    wipe: bool | None = None,
) -> AmigaDOSVolume:
    """Write a brand-new empty volume across the whole of ``reader``.

    ``wipe`` clears every block first. Left unset, a volume the size of a
    floppy or a small drive is cleared and a larger one is not.
    """
    if dos_type not in DOS_TYPES:
        raise ConfigurationError(f"DOS type {dos_type!r} is not supported.")
    if DOS_TYPES[dos_type] not in WRITABLE_FORMATS:
        raise ConfigurationError(
            f"{DOS_TYPES[dos_type]} volumes can be read but not created by this build."
        )
    block_size = reader.block_size
    total = reader.total_blocks
    reserved = geometry.reserved if geometry else RESERVED_BLOCKS
    if total <= reserved + 4:
        raise ConfigurationError("The requested volume is too small to format.")
    blank = b"\0" * block_size
    if wipe is None:
        wipe = total <= WIPE_LIMIT_BLOCKS
    if wipe:
        run = blank * 2048
        for offset in range(0, total * block_size, len(run)):
            reader.write_range(offset, run[: total * block_size - offset])
    else:
        reader.write_range(0, b"\0" * min(WIPE_HEAD_BYTES, total * block_size))

    hash_table_size = block_size // 4 - 56
    # Half way through the whole volume: 880 on a double-density floppy, 1760
    # on a high-density one, which is where AmigaDOS and every tool that reads
    # an ADF expect to find it.
    root_block = total // 2
    covered = total - reserved
    longs_per_page = block_size // 4 - 1
    bits_per_page = longs_per_page * 32
    page_count = (covered + bits_per_page - 1) // bits_per_page
    # The root block names the first 25 bitmap blocks. A volume past about
    # 50 MB needs more, and those are named by bitmap extension blocks, each
    # holding 127 block numbers and the number of the next extension.
    extension_count = -(-max(0, page_count - ROOT_BITMAP_PAGES) // longs_per_page)
    bitmap_blocks = [root_block + 1 + index for index in range(page_count)]
    extension_blocks = [
        root_block + 1 + page_count + index for index in range(extension_count)
    ]
    # A directory-cache volume starts with one empty cache block for the root,
    # placed straight after the bitmap.
    after_bitmap = root_block + 1 + page_count + extension_count
    cache_block = after_bitmap if is_dircache(dos_type) else 0
    if after_bitmap + 1 >= total:
        raise ConfigurationError("The requested volume is too small to format.")

    root = bytearray(block_size)
    put_long(root, OFF_TYPE, T_HEADER)
    put_long(root, OFF_HT_SIZE, hash_table_size)
    put_long(root, _tail(block_size, 200), 0xFFFFFFFF)
    for index, block in enumerate(bitmap_blocks[:ROOT_BITMAP_PAGES]):
        put_long(root, _tail(block_size, 196) + index * 4, block)
    if extension_blocks:
        put_long(root, _tail(block_size, 96), extension_blocks[0])
    now = datetime.now(timezone.utc)
    days, mins, ticks = datetime_to_datestamp(now)
    for offset in (_tail(block_size, 92), _tail(block_size, 40), _tail(block_size, 28)):
        put_long(root, offset, days)
        put_long(root, offset + 4, mins)
        put_long(root, offset + 8, ticks)
    write_bstr(root, _tail(block_size, 80), validate_name(label), MAX_NAME)
    used = {root_block, *bitmap_blocks, *extension_blocks}
    if cache_block:
        used.add(cache_block)
        put_long(root, _tail(block_size, 8), cache_block)
        cache = bytearray(block_size)
        put_long(cache, OFF_TYPE, T_DIRCACHE)
        put_long(cache, OFF_HEADER_KEY, cache_block)
        put_long(cache, 8, root_block)
        reader.write_block(cache_block, bytes(apply_checksum(cache)))
    if is_long_names(dos_type):
        # The long-name variants repeat the DOS type in the root block and
        # keep a running count of the blocks in use there.
        put_long(root, _tail(block_size, 16), int.from_bytes(dos_type, "big"))
        put_long(root, _tail(block_size, 44), len(used))
    put_signed_long(root, _tail(block_size, 4), ST_ROOT)
    reader.write_block(root_block, bytes(apply_checksum(root)))

    remaining = bitmap_blocks[ROOT_BITMAP_PAGES:]
    for index, extension in enumerate(extension_blocks):
        page = bytearray(block_size)
        names = remaining[index * longs_per_page : (index + 1) * longs_per_page]
        for position, block in enumerate(names):
            put_long(page, position * 4, block)
        if index + 1 < len(extension_blocks):
            put_long(page, block_size - 4, extension_blocks[index + 1])
        reader.write_block(extension, bytes(page))

    # Nearly every bitmap block of a new volume says the same thing, that all
    # the blocks it covers are free, so that one is packed once. Only the
    # blocks covering something in use, and the last, are worked out.
    all_free = _packed_bitmap_page(b"\x01" * bits_per_page, block_size)
    occupied: dict[int, list[int]] = {}
    for block in used:
        position = block - reserved
        occupied.setdefault(position // bits_per_page, []).append(position % bits_per_page)
    for page_index, page_block in enumerate(bitmap_blocks):
        first = page_index * bits_per_page
        length = min(bits_per_page, covered - first)
        taken = occupied.get(page_index)
        if taken is None and length == bits_per_page:
            reader.write_block(page_block, all_free)
            continue
        bits = bytearray(b"\x01" * length)
        for position in taken or ():
            bits[position] = 0
        reader.write_block(page_block, _packed_bitmap_page(bytes(bits), block_size))

    boot_first = bytearray(block_size)
    boot_first[0:4] = dos_type
    if bootable:
        boot_first[12 : 12 + len(STANDARD_BOOT_CODE)] = STANDARD_BOOT_CODE
    checksum = _boot_checksum(bytes(boot_first) + blank)
    struct.pack_into(">I", boot_first, 4, checksum)
    reader.write_block(0, bytes(boot_first))
    reader.write_block(1, blank)
    reader.flush()
    return AmigaDOSVolume(reader, geometry)


__all__ = [
    "AmigaDOSVolume",
    "Entry",
    "OFS_DATA_HEADER",
    "STANDARD_BOOT_CODE",
    "Stat",
    "format_volume",
    "join_path",
    "split_path",
    "validate_name",
]
