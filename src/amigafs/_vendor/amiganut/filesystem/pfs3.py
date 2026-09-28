"""The Professional File System (PFS3), as pfs3aio and PFS3 2.x write it.

PFS3 keeps every structure but file data in a reserved area at the start of
the partition, and addresses the whole volume in logical blocks, 512 bytes
unless the partition asks for larger ones. The structures are these, all in
big-endian words and longs:

* The boot block, block 0, carries the id ``PFS\\1`` and nothing else.
* The root block, block 2, is the first 512 bytes of the root cluster. It
  names the volume, records the option bits that say which features the
  volume uses, the size of a reserved block (1024 bytes on most volumes, and
  larger than a logical block, which catches out anyone assuming otherwise),
  the free-block counts, and pointers to the index blocks. The reserved
  bitmap, one bit per reserved block, follows it inside the same cluster.
* The root block extension (``EX``) holds the allocation roving pointers,
  the longest name the volume allows, the super index, the deleted-files
  directory and a postponed-operation record.
* Anode blocks (``AB``). An anode is an extent: a run length, a first block
  and the number of the next anode. A file is its chain of anodes; a
  directory is a chain of one-block anodes, one per directory block. Anode
  numbers are a block sequence number in the upper word and a slot in the
  lower on every volume with ``SPLITTED_ANODES``.
* Index blocks (``IB``) list the anode blocks by sequence number. A small
  volume lists its index blocks in the root block; above about 5 GB the
  volume switches to super index mode and lists them through super blocks
  (``SB``) named by the extension instead.
* Bitmap blocks (``BM``), listed by bitmap index blocks (``MI``) named by the
  root block. A set bit means free, most significant bit first, and bit 0 is
  the first block after the reserved area, not block 0 of the partition.
* Directory blocks (``DB``). Each records the anode of its directory and of
  the parent directory, then variable-length entries: size, type, anode,
  file size, date, protection, name and comment, and with
  ``DIR_EXTENSION`` a packed set of extra fields carrying hard-link chains,
  the upper protection bits and the upper bits of a large file's size.
* Deleted-files directory blocks (``DD``), each listing 31 recently deleted
  files whose anodes are kept so they can be recovered.

PFS3 has no checksums. It keeps a volume consistent by copy-on-write: a
changed reserved block is written to a newly allocated reserved block, the
block that points to it changes too, and so on up to the root block, which
is the only block ever overwritten in place. Until the root block is written
the old tree is untouched, so a crash leaves the volume as it was. This
module reads that tree; ``pfs3_write`` changes it the same way.

The structure definitions were taken from ``blocks.h`` of the pfs3aio
sources and ``pfs3.h`` of PFSDoctor, both published under a BSD licence.
No code is shared with them; only the on-disk format is.
"""

from __future__ import annotations

import collections.abc
from dataclasses import dataclass
from datetime import datetime

from ..errors import DataError
from ..file import AmigaMeta
from .amigados import Entry, Stat, join_path, split_path
from .blocks import ST_FILE, ST_LINKDIR, ST_LINKFILE, ST_ROOT, ST_SOFTLINK, ST_USERDIR, BlockReader
from .pfs3_blocks import (
    ANODE_ROOTDIR,
    ANODE_SIZE,
    ANODEBLOCK_HEADER,
    ANODEBLOCK_ID,
    BITMAPBLOCK_ID,
    BITMAPINDEX_ID,
    BLOCK_HEADER,
    DEFAULT_FNSIZE,
    DIRBLOCK_ID,
    EMPTY_BLOCKNR,
    EXT_FNSIZE,
    EXT_ROOT_DATE,
    EXT_SUPERINDEX,
    EXT_TOBEDONE,
    EXTENSION_ID,
    INDEXBLOCK_ID,
    KNOWN_MODES,
    MAXBITMAPINDEX,
    MAXSMALLBITMAPINDEX,
    MAXSMALLINDEXNR,
    MAXSUPER,
    MODE_DIR_EXTENSION,
    MODE_EXTENSION,
    MODE_HARDDISK,
    MODE_LARGEFILE,
    MODE_SIZEFIELD,
    MODE_SPLITTED_ANODES,
    MODE_SUPERINDEX,
    PFS1_ID,
    POSTPONED_OPERATIONS,
    RESERVED_BITMAP,
    ROOT_ALWAYSFREE,
    ROOT_BITMAPINDEX,
    ROOT_BLOCKSFREE,
    ROOT_DISKNAME,
    ROOT_DISKSIZE,
    ROOT_EXTENSION,
    ROOT_FIRSTRESERVED,
    ROOT_IDS,
    ROOT_LASTRESERVED,
    ROOT_OPTIONS,
    ROOT_RBLKCLUSTER,
    ROOT_RESERVED_BLKSIZE,
    ROOT_SIZE,
    ROOT_SMALL_INDEX,
    ROOTBLOCK,
    ST_ROLLOVERFILE,
    DirEntry,
    count_set_bits,
    fold,
    iter_block_entries,
    u16,
    u32,
    unpack_anode,
)
from .pfs3_write import PFS3Writer

#: Directory entry types the handler writes.
KNOWN_TYPES = (ST_USERDIR, ST_SOFTLINK, ST_LINKDIR, ST_FILE, ST_LINKFILE, ST_ROLLOVERFILE)


@dataclass(frozen=True)
class Anode:
    """One extent: ``clustersize`` blocks from ``blocknr``, then anode ``next``."""

    number: int
    clustersize: int
    blocknr: int
    next: int

    @property
    def is_free(self) -> bool:
        return not (self.clustersize or self.blocknr or self.next)


class PFS3Volume(PFS3Writer):
    """One PFS3 partition, read through a window onto its image or drive."""

    format = "PFS3"

    #: Reserved blocks kept in memory. A drive attached over USB answers each
    #: read slowly, and a directory walk revisits the same anode and index
    #: blocks many times.
    CACHE_BLOCKS = 8192

    def __init__(self, reader: BlockReader):
        self.sector_reader = reader
        self.writable = reader.writable
        self.block_size, base, length = self._find_root(reader)
        self.blocks = reader.reopen(
            offset=base,
            length=length,
            block_size=self.block_size,
        )
        self._cache: dict[int, bytes] = {}
        self._dirty: dict[int, bytearray] = {}
        try:
            self._load_root()
        except BaseException:
            self.blocks.close()
            raise
        self._reset_changes()

    # ---- locating the volume -----------------------------------------
    @staticmethod
    def _find_root(reader: BlockReader) -> tuple[int, int, int]:
        """Work out the logical block size and the byte range the volume spans.

        pfs3aio tries the partition's own block size and then falls back to
        512 bytes, because volumes formatted before large blocks existed use
        512 whatever the partition says. The volume starts at the first
        whole logical block of the partition, counted from the start of the
        drive, and ends at the last whole one.
        """
        for size in (512, 1024, 2048, 4096):
            if size < reader.block_size:
                continue
            base = -(-reader.offset // size) * size
            end = (reader.offset + reader.length) // size * size
            if end - base < size * 8:
                continue
            skip = base - reader.offset
            boot = reader.read_range(skip, 4)
            root = reader.read_range(skip + ROOTBLOCK * size, ROOT_SIZE)
            if boot not in ROOT_IDS or root[:4] not in ROOT_IDS:
                continue
            if not u32(root, ROOT_OPTIONS) or not u16(root, ROOT_RESERVED_BLKSIZE):
                continue
            return size, base, end - base
        raise DataError("No PFS3 root block was found at the start of the partition.")

    def _load_root(self) -> None:
        head = self.blocks.read_range(ROOTBLOCK * self.block_size, ROOT_SIZE)
        cluster = u16(head, ROOT_RBLKCLUSTER)
        if not 1 <= cluster <= 521:
            raise DataError(f"The PFS3 root cluster size of {cluster} blocks is not valid.")
        if (ROOTBLOCK + cluster) * self.block_size > self.blocks.length:
            raise DataError("The PFS3 root cluster runs past the end of the partition.")
        self._root = bytearray(self.blocks.read_range(ROOTBLOCK * self.block_size, cluster * self.block_size))
        self._apply_root()

    def _apply_root(self) -> None:
        root = self._root
        self.disktype = bytes(root[:4])
        self.options = u32(root, ROOT_OPTIONS)
        self.reserved_blksize = u16(root, ROOT_RESERVED_BLKSIZE)
        rbs = self.reserved_blksize
        if rbs < self.block_size or rbs > 4096 or rbs & (rbs - 1):
            raise DataError(f"The PFS3 reserved block size of {rbs} bytes is not valid.")
        if self.disktype == PFS1_ID and (self.options & MODE_LARGEFILE or rbs > 1024):
            raise DataError("This volume says PFS\\1 but uses PFS\\2 features, so PFS3 would not mount it.")
        self.rescluster = rbs // self.block_size
        self.firstreserved = u32(root, ROOT_FIRSTRESERVED)
        self.lastreserved = u32(root, ROOT_LASTRESERVED)
        self.bitmap_start = self.lastreserved + 1
        self.split_anodes = bool(self.options & MODE_SPLITTED_ANODES)
        self.dir_extension = bool(self.options & MODE_DIR_EXTENSION)
        self.supermode = bool(self.options & MODE_SUPERINDEX)
        self.largefile = bool(self.options & MODE_LARGEFILE) and self.dir_extension
        self.anodes_per_block = (rbs - ANODEBLOCK_HEADER) // ANODE_SIZE
        self.index_per_block = (rbs - BLOCK_HEADER) // 4
        self.longs_per_bitmap = rbs // 4 - 3
        self.bits_per_bitmap = self.longs_per_bitmap * 32
        self.num_reserved = (self.lastreserved - self.firstreserved + 1) // self.rescluster
        total = self.blocks.total_blocks
        if self.options & MODE_SIZEFIELD:
            # The size field counts the drive's own sectors across the whole
            # partition, which is what a partition table describes.
            declared = u32(root, ROOT_DISKSIZE) * self.sector_reader.block_size
            if declared > self.sector_reader.length:
                raise DataError(
                    f"The PFS3 volume declares {declared:,} bytes but its partition holds "
                    f"only {self.sector_reader.length:,}."
                )
            total = min(total, declared // self.block_size)
        self.total_blocks = total
        if not self.firstreserved or self.lastreserved >= total or self.firstreserved > self.lastreserved:
            raise DataError("The PFS3 reserved area lies outside the partition.")
        if self.num_reserved > (len(self._root) - RESERVED_BITMAP) * 8:
            raise DataError("The PFS3 reserved bitmap does not fit its root cluster.")
        longs = -(-(total - self.bitmap_start) // 32)
        self.bitmap_blocks_needed = -(-longs // self.longs_per_bitmap)
        self._ext_committed = None
        if self.extension_block:
            ext = self.blocks.read_range(self.extension_block * self.block_size, rbs)
            if ext[:2] != EXTENSION_ID:
                raise DataError("The PFS3 root block extension is missing or damaged.")
            self._ext_committed = ext
        if self.supermode and not self.extension_block:
            raise DataError("A PFS3 volume in super index mode needs its root block extension.")
        ext = self._ext_committed
        self.fnsize = (u16(ext, EXT_FNSIZE) if ext else 0) or DEFAULT_FNSIZE
        self.read_only_reason = ""
        if not self.options & MODE_HARDDISK:
            self.read_only_reason = "This is a floppy-mode PFS volume, which this build only reads."
            raise DataError("Floppy-mode PFS volumes are not supported.")
        if self.options & ~KNOWN_MODES:
            self.read_only_reason = "This PFS3 volume uses options this build does not know."
        elif self.pending_operation():
            self.read_only_reason = (
                "PFS3 did not finish its last change (" + self.pending_operation() + "). "
                "Mount the volume on an Amiga so PFS3 can complete it before changing it here."
            )
        self.read_only = not self.writable or bool(self.read_only_reason)

    @property
    def extension_block(self) -> int:
        """Where the root block extension is now; a change moves it."""
        if not self.options & MODE_EXTENSION:
            return 0
        return u32(self._root, ROOT_EXTENSION)

    def pending_operation(self) -> str:
        """Describe the postponed operation the extension records, if any."""
        ext = self._ext_committed
        if not ext:
            return ""
        operation = u32(ext, EXT_TOBEDONE)
        if not operation:
            return ""
        return POSTPONED_OPERATIONS.get(operation, f"unknown operation {operation}")

    # ---- block access -------------------------------------------------
    def _reserved(self, number: int) -> bytes:
        """Return one reserved block, as changed so far if it is being changed."""
        pending = self._dirty.get(number)
        if pending is not None:
            return pending
        cached = self._cache.get(number)
        if cached is not None:
            return cached
        if not self.firstreserved <= number <= self.lastreserved:
            raise DataError(f"PFS3 reserved block {number} lies outside the reserved area.")
        data = self.blocks.read_range(number * self.block_size, self.reserved_blksize)
        if len(self._cache) >= self.CACHE_BLOCKS:
            self._cache.pop(next(iter(self._cache)))
        self._cache[number] = data
        return data

    def _typed(self, number: int, block_id: bytes, what: str = "") -> bytes:
        data = self._reserved(number)
        if data[:2] != block_id:
            found = bytes(data[:2]).decode("latin-1", "replace")
            raise DataError(
                f"PFS3 block {number}{' (' + what + ')' if what else ''} should be "
                f"{block_id.decode()} but holds {found!r}."
            )
        return data

    def _ext(self) -> bytes | None:
        """The root block extension as it stands, changed or not."""
        number = u32(self._root, ROOT_EXTENSION) if self.options & MODE_EXTENSION else 0
        if not number:
            return None
        return self._typed(number, EXTENSION_ID, "root block extension")

    # ---- index trees --------------------------------------------------
    def _superblock_number(self, super_nr: int) -> int:
        ext = self._ext()
        if ext is None or super_nr > MAXSUPER:
            return 0
        return u32(ext, EXT_SUPERINDEX + 4 * super_nr)

    def _index_block_number(self, index_nr: int) -> int:
        """Where anode index block ``index_nr`` is, or 0 if it does not exist."""
        if self.supermode:
            super_nr, slot = divmod(index_nr, self.index_per_block)
            number = self._superblock_number(super_nr)
            if not number:
                return 0
            return u32(self._typed(number, b"SB", "super block"), BLOCK_HEADER + 4 * slot)
        if index_nr > MAXSMALLINDEXNR:
            return 0
        return u32(self._root, ROOT_SMALL_INDEX + 4 * index_nr)

    def _anode_block_number(self, seqnr: int) -> int:
        """Where anode block ``seqnr`` is, or 0 if it does not exist."""
        index_nr, slot = divmod(seqnr, self.index_per_block)
        number = self._index_block_number(index_nr)
        if not number:
            return 0
        return u32(self._typed(number, INDEXBLOCK_ID, "anode index"), BLOCK_HEADER + 4 * slot)

    def _bitmap_index_number(self, index_nr: int) -> int:
        limit = MAXBITMAPINDEX if self.supermode else MAXSMALLBITMAPINDEX
        if index_nr > limit:
            return 0
        return u32(self._root, ROOT_BITMAPINDEX + 4 * index_nr)

    def _bitmap_block_number(self, seqnr: int) -> int:
        index_nr, slot = divmod(seqnr, self.index_per_block)
        number = self._bitmap_index_number(index_nr)
        if not number:
            return 0
        return u32(self._typed(number, BITMAPINDEX_ID, "bitmap index"), BLOCK_HEADER + 4 * slot)

    # ---- anodes -------------------------------------------------------
    def _split(self, number: int) -> tuple[int, int]:
        if self.split_anodes:
            return number >> 16, number & 0xFFFF
        return divmod(number, self.anodes_per_block)

    def _join(self, seqnr: int, slot: int) -> int:
        if self.split_anodes:
            return (seqnr << 16) | slot
        return seqnr * self.anodes_per_block + slot

    def anode(self, number: int) -> Anode:
        seqnr, slot = self._split(number)
        if slot >= self.anodes_per_block:
            raise DataError(f"PFS3 anode {number:#x} names a slot beyond its block.")
        block = self._anode_block_number(seqnr)
        if not block:
            raise DataError(f"PFS3 anode {number:#x} lies in an anode block that does not exist.")
        data = self._typed(block, ANODEBLOCK_ID, "anode block")
        clustersize, blocknr, following = unpack_anode(data, ANODEBLOCK_HEADER + slot * ANODE_SIZE)
        return Anode(number, clustersize, blocknr, following)

    def anode_chain(self, number: int) -> list[Anode]:
        chain: list[Anode] = []
        seen: set[int] = set()
        while number:
            if number in seen:
                raise DataError(f"The PFS3 anode chain through {number:#x} loops back on itself.")
            seen.add(number)
            node = self.anode(number)
            chain.append(node)
            number = node.next
        return chain

    def extents(self, number: int) -> list[tuple[int, int]]:
        """A file's data as (first block, block count) runs, in order."""
        return [
            (node.blocknr, node.clustersize)
            for node in self.anode_chain(number)
            if node.clustersize and node.blocknr not in (0, EMPTY_BLOCKNR)
        ]

    # ---- directories --------------------------------------------------
    def directory_blocks(self, main: int):
        """Yield (anode number, block number, contents) for each block of a directory."""
        for node in self.anode_chain(main):
            yield node.number, node.blocknr, self._typed(node.blocknr, DIRBLOCK_ID, "directory block")

    def entries(self, main: int):
        """Yield every entry of the directory whose chain starts at anode ``main``."""
        for _number, block, data in self.directory_blocks(main):
            yield from iter_block_entries(
                data, dir_extension=self.dir_extension, largefile=self.largefile,
                block=block, directory=main,
            )

    def find(self, main: int, name: str) -> DirEntry | None:
        try:
            wanted = fold(name.encode("latin-1"))
        except UnicodeEncodeError:
            return None
        for entry in self.entries(main):
            if len(entry.raw_name) == len(wanted) and fold(entry.raw_name) == wanted:
                return entry
        return None

    def find_by_anode(self, main: int, anode: int) -> DirEntry | None:
        for entry in self.entries(main):
            if entry.anode == anode:
                return entry
        return None

    def root_entry(self) -> DirEntry:
        ext = self._ext()
        days = mins = ticks = 0
        if ext is not None:
            days, mins, ticks = (u16(ext, EXT_ROOT_DATE + 2 * index) for index in range(3))
        else:
            days, mins, ticks = (u16(self._root, 12 + 2 * index) for index in range(3))
        return DirEntry(
            name=self.title, raw_name=b"", type=ST_ROOT, anode=ANODE_ROOTDIR, fsize=0,
            days=days, mins=mins, ticks=ticks, protection=0, comment="",
        )

    def resolve(self, path: str | None) -> tuple[DirEntry, list[str]]:
        parts = split_path(path)
        current = self.root_entry()
        for index, part in enumerate(parts):
            if not current.is_dir:
                raise DataError(f"{join_path(parts[:index])} is not a directory.")
            found = self.find(current.listing_anode, part)
            if found is None:
                raise DataError(f"Path not found: {join_path(parts[: index + 1])}")
            current = found
        return current, parts

    # ---- volume identity ------------------------------------------------
    @property
    def title(self) -> str:
        length = min(self._root[ROOT_DISKNAME], 31)
        return bytes(self._root[ROOT_DISKNAME + 1 : ROOT_DISKNAME + 1 + length]).decode("latin-1")

    def size_bytes(self) -> int:
        return self.total_blocks * self.block_size

    def free_bytes(self) -> int:
        """Free space as ``Info`` shows it: PFS3 keeps a twentieth in hand."""
        usable = u32(self._root, ROOT_BLOCKSFREE) - u32(self._root, ROOT_ALWAYSFREE)
        return max(usable, 0) * self.block_size

    def used_bytes(self) -> int:
        return self.size_bytes() - self.free_bytes()

    def describe(self) -> dict:
        return {
            "disktype": self.disktype.decode("latin-1"),
            "blockSize": self.block_size,
            "reservedBlockSize": self.reserved_blksize,
            "blocks": self.total_blocks,
            "reservedBlocks": self.num_reserved,
            "options": self.options,
            "nameLength": self.fnsize - 1,
        }

    @property
    def max_name_length(self) -> int:
        return self.fnsize - 1

    @property
    def name_limit(self) -> int:
        """The longest name this volume takes, which its format chose."""
        return self.max_name_length

    # ---- traversal ------------------------------------------------------
    def exists(self, path: str | None) -> bool:
        try:
            self.resolve(path)
        except DataError:
            return False
        return True

    def stat(self, path: str | None) -> Stat:
        found, parts = self.resolve(path)
        size = 0 if found.is_dir else found.size
        return Stat(
            name=parts[-1] if parts else self.title,
            path=join_path(parts),
            is_dir=found.is_dir,
            length=size,
            blocks=-(-size // self.block_size) if size else 1,
            block=found.anode,
            secondary_type=found.type,
        )

    def iter_entries(self, path: str | None = None):
        directory, parts = self.resolve(path)
        if not directory.is_dir:
            raise DataError(f"{join_path(parts)} is not a directory.")
        prefix = join_path(parts)
        for child in self.entries(directory.listing_anode):
            yield Entry(
                name=child.name,
                path=f"{prefix}/{child.name}" if prefix else child.name,
                is_dir=child.is_dir,
                length=0 if child.is_dir else child.size,
                block=child.anode,
                secondary_type=child.type,
            )

    # ---- reading --------------------------------------------------------
    def read_bytes(self, path: str) -> bytes:
        found, parts = self.resolve(path)
        if found.is_dir:
            raise DataError(f"{join_path(parts)} is not a file.")
        if found.type == ST_LINKFILE:
            # A hard link has an anode of its own that only records where the
            # object and the link live; the data belongs to the object.
            target = self.link_target_entry(found)
            if target is None:
                return self.read_extent_data(found.extra.link, found.size)
            found = target
        data = self.read_extent_data(found.anode, found.size)
        if found.type == ST_ROLLOVERFILE and found.extra.virtualsize:
            # A rollover file is a ring buffer: its readable contents start at
            # the roll pointer and wrap round the end of the stored data.
            start = found.extra.rollpointer % max(len(data), 1)
            data = (data[start:] + data[:start])[: found.extra.virtualsize]
        return data

    def read_extent_data(self, anode: int, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        for first, count in self.extents(anode) if size else ():
            if remaining <= 0:
                break
            if first + count > self.total_blocks:
                raise DataError("A PFS3 file extends beyond the end of its volume.")
            wanted = min(count * self.block_size, remaining)
            chunks.append(self.blocks.read_range(first * self.block_size, wanted))
            remaining -= wanted
        if remaining > 0:
            raise DataError(
                f"The file declares {size:,} bytes but its anodes hold only "
                f"{size - remaining:,}. The file is truncated."
            )
        return b"".join(chunks)

    def link_target_entry(self, link: DirEntry) -> DirEntry | None:
        """The entry a hard link points to, found through its link node."""
        try:
            node = self.anode(link.anode)
            return self.find_by_anode(node.clustersize, link.extra.link)
        except DataError:
            return None

    # ---- metadata -------------------------------------------------------
    def amiga_meta(self, path: str) -> AmigaMeta:
        found, _parts = self.resolve(path)
        return AmigaMeta(
            protection=found.full_protection,
            comment=found.comment,
            datestamp=found.datestamp,
        )

    def access(self, path: str):
        return self.amiga_meta(path).access

    def comment(self, path: str) -> str:
        return self.amiga_meta(path).comment

    def datestamp(self, path: str) -> datetime:
        return self.amiga_meta(path).datestamp

    def boot_option(self) -> int:
        """PFS3 has no boot block options; a partition boots through the RDB."""
        return 0

    # ---- free space -----------------------------------------------------
    def _bitmap_payload(self, seqnr: int) -> bytes:
        number = self._bitmap_block_number(seqnr)
        if not number:
            raise DataError(f"PFS3 bitmap block {seqnr} is missing from its index.")
        data = self._typed(number, BITMAPBLOCK_ID, "bitmap block")
        if u32(data, 8) != seqnr:
            raise DataError(f"PFS3 bitmap block {number} carries the wrong sequence number.")
        return data[BLOCK_HEADER : BLOCK_HEADER + self.longs_per_bitmap * 4]

    def reserved_bitmap(self) -> bytes:
        return bytes(self._root[RESERVED_BITMAP : RESERVED_BITMAP + (self.num_reserved + 7) // 8])

    def free_map(self) -> "PFS3FreeMap":
        """Return one flag per block, True when the block is free.

        The map is read a bitmap block at a time as it is walked, so it costs
        the same memory on a 60 GB partition as on a floppy-sized one.
        """
        return PFS3FreeMap(self)

    def free_block_count(self) -> int:
        """Count free data blocks from the bitmap rather than the root's total."""
        free = 0
        data_blocks = self.total_blocks - self.bitmap_start
        for seqnr in range(self.bitmap_blocks_needed):
            covered = min(self.bits_per_bitmap, data_blocks - seqnr * self.bits_per_bitmap)
            free += count_set_bits(self._bitmap_payload(seqnr), 0, covered)
        return free

    # ---- maintenance ----------------------------------------------------
    def validate(self) -> list[str]:
        """Check the volume the way PFSDoctor does, and report what is wrong.

        The walk covers the anode index tree, the bitmap index tree, the
        deleted-files directory and every directory and file, then checks
        that they agree: each directory block names its own directory and
        parent, each file has exactly the blocks its size needs, no block
        belongs to two files, nothing in use is marked free in either bitmap,
        nothing marked in use is unreferenced, and the root's free counts
        match its bitmaps. It reads a block at a time, so a large partition
        is checked in bounded memory.
        """
        from .pfs3_check import check_volume

        return check_volume(self)

    def flush(self) -> None:
        self.blocks.flush()

    def close(self) -> None:
        try:
            self.blocks.close()
        finally:
            self.sector_reader.close()


class PFS3FreeMap(collections.abc.Sequence):
    """A lazy, read-only list of free-block flags for a PFS3 volume.

    Blocks in the reserved area follow the reserved bitmap, so a reserved
    block counts as free when the handler could hand it out; the boot blocks
    and the root cluster are always in use. Data blocks follow the main
    bitmap, read one bitmap block at a time and only when asked for.
    """

    def __init__(self, volume: PFS3Volume):
        self.volume = volume
        self._reserved = volume.reserved_bitmap()
        self._page = -1
        self._payload = b""

    def __len__(self) -> int:
        return self.volume.total_blocks

    def _flag(self, block: int) -> bool:
        volume = self.volume
        if block < volume.firstreserved:
            return False
        if block < volume.bitmap_start:
            index = (block - volume.firstreserved) // volume.rescluster
            return bool(self._reserved[index // 8] & (0x80 >> (index % 8)))
        bit = block - volume.bitmap_start
        page, bit = divmod(bit, volume.bits_per_bitmap)
        if page != self._page:
            self._payload = volume._bitmap_payload(page)
            self._page = page
        return bool(self._payload[bit // 8] & (0x80 >> (bit % 8)))

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self._flag(block) for block in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError("block number out of range")
        return self._flag(index)

    def __iter__(self):
        for block in range(len(self)):
            yield self._flag(block)


__all__ = ["Anode", "PFS3FreeMap", "PFS3Volume"]
