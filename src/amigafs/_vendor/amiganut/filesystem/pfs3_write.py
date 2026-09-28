"""Changing a PFS3 volume the way the handler itself would.

Every public change is gathered in memory and then committed with the
handler's own copy-on-write protocol, in this order:

1. File data is written into blocks that are free in the committed bitmap,
   and flushed to the medium.
2. Blocks freed by the change are marked free in the bitmap. Until now they
   were only listed, so the data written in step 1 can never land on a block
   the committed tree still uses.
3. Every changed reserved block that already existed is moved to a newly
   allocated reserved block, and the block that points to it is changed to
   match: a directory block through its anode, an anode block through its
   index block, an index block through its super block or the root, a bitmap
   block through its bitmap index, and the extension through the root. The
   moves run from the leaves upwards, so each parent is moved after the
   children that changed it.
4. The moved and new blocks are written, stamped with the new datestamp, and
   flushed. None of them is referenced by the committed tree, which is
   therefore still whole.
5. The root cluster, with the reserved bitmap that marks the new blocks as
   used, is written over the old one and flushed. This is the commit.
6. The old places of the moved blocks are released in the reserved bitmap
   and the root cluster is written once more.

Power lost before step 5 leaves the volume exactly as it was; power lost
after it leaves the new volume, at worst with a few reserved blocks still
marked in use until PFSDoctor reclaims them. That is the guarantee pfs3aio
itself gives.

Allocation follows the handler too. Reserved blocks come from the reserved
bitmap from its roving pointer on; anodes from the first anode block with a
free slot from the anode roving pointer, keeping the last six slots of each
block for chained anodes; data from the main bitmap from its roving pointer,
never dipping into the twentieth of the volume the handler keeps free.

Deleting a file frees its blocks and anodes at once. pfs3aio would instead
move a deleted file into the deleted-files directory when that is enabled,
keeping its anodes so it can be recovered until the slot is reused; that is
a convenience on the Amiga rather than part of the volume's consistency, and
entries already in that directory are left exactly as they are.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime

from ..errors import DataError
from ..file import AmigaMeta, DEFAULT_PROTECTION
from .amigados import join_path, split_path
from .blocks import ST_FILE, ST_USERDIR, BlockReader
from .pfs3_blocks import (
    ANODE_ROOTDIR,
    ANODE_SIZE,
    ANODE_USERFIRST,
    ANODEBLOCK_HEADER,
    ANODEBLOCK_ID,
    BITMAPBLOCK_ID,
    BITMAPINDEX_ID,
    BLOCK_HEADER,
    DEFAULT_FNSIZE,
    DELDIR_ID,
    DIRBLOCK_HEADER,
    DIRBLOCK_ID,
    EMPTY_BLOCKNR,
    EXT_CURRANSEQNR,
    EXT_DATESTAMP,
    EXT_DELDIR,
    EXT_DELDIRSIZE,
    EXT_FNSIZE,
    EXT_RESERVED_ROVING,
    EXT_ROOT_DATE,
    EXT_ROVINGBIT,
    EXT_SUPERINDEX,
    EXT_VERSION,
    EXT_VOLUME_DATE,
    EXTENSION_ID,
    FIBF_ARCHIVE,
    FIBF_DELETE,
    INDEXBLOCK_ID,
    MAX_DISKNAME,
    MAX_FNSIZE,
    MAXBITMAPINDEX,
    MAXDISKSIZE1K,
    MAXDISKSIZE2K,
    MAXDISKSIZE4K,
    MAXSMALLBITMAPINDEX,
    MAXSMALLDISK,
    MAXSMALLINDEXNR,
    MAXSUPER,
    MODE_DATESTAMP,
    MODE_DELDIR,
    MODE_DIR_EXTENSION,
    MODE_EXTENSION,
    MODE_EXTROVING,
    MODE_HARDDISK,
    MODE_LONGFN,
    MODE_SIZEFIELD,
    MODE_SPLITTED_ANODES,
    MODE_SUPERDELDIR,
    MODE_SUPERINDEX,
    PFS1_ID,
    PFS2_ID,
    RESERVED_BITMAP,
    RESERVEDANODES,
    RESFREE_THRESHOLD,
    REVNUM,
    ROOT_ALWAYSFREE,
    ROOT_BITMAPINDEX,
    ROOT_BLOCKSFREE,
    ROOT_CREATION,
    ROOT_DATESTAMP,
    ROOT_DELDIR,
    ROOT_DISKNAME,
    ROOT_DISKSIZE,
    ROOT_EXTENSION,
    ROOT_FIRSTRESERVED,
    ROOT_LASTRESERVED,
    ROOT_OPTIONS,
    ROOT_PROTECTION,
    ROOT_RBLKCLUSTER,
    ROOT_RESERVED_BLKSIZE,
    ROOT_RESERVED_FREE,
    ROOT_ROVING_PTR,
    ROOT_SMALL_INDEX,
    ROOTBLOCK,
    SUPERBLOCK_ID,
    VERNUM,
    DirEntry,
    ExtraFields,
    calc_num_reserved,
    count_set_bits,
    datetime_to_triple,
    encode_comment,
    encode_entry,
    encode_name,
    entries_end,
    first_clear_bit,
    first_set_bit,
    pack_anode,
    put_u16,
    put_u32,
    reencode,
    root_cluster_blocks,
    set_bits,
    u16,
    u32,
)

#: A long change, such as deleting a large tree, is committed in stages once
#: this many reserved blocks are waiting, so it never holds more in memory.
CHECKPOINT_BLOCKS = 512

#: File data is written in pieces of at most this many bytes.
WRITE_CHUNK = 4 * 1024 * 1024

#: The order in which changed blocks are moved: each kind's parent comes later.
RELOCATION_ORDER = (
    DIRBLOCK_ID, ANODEBLOCK_ID, INDEXBLOCK_ID, SUPERBLOCK_ID, DELDIR_ID,
    BITMAPBLOCK_ID, BITMAPINDEX_ID, EXTENSION_ID,
)

_FREE_ANODE = bytes(ANODE_SIZE)


class PFS3Writer:
    """The change half of ``PFS3Volume``; it relies on the reader's attributes."""

    # ---- change state -------------------------------------------------
    def _reset_changes(self) -> None:
        self._dirty: dict[int, bytearray] = {}
        self._fresh: set[int] = set()
        self._data_frees: list[tuple[int, int]] = []
        self._reserved_frees: list[int] = []
        self._depth = 0
        self._root_before: bytes | None = None
        self._full_anode_blocks: set[int] = set()
        self._max_anode_seq: int | None = None
        ext = self._ext_committed
        if ext is not None:
            roving = u32(ext, EXT_RESERVED_ROVING)
            # Before EXTROVING the reserved roving pointer counted longs.
            self._res_roving = roving if self.options & MODE_EXTROVING else roving * 32
            self._rovingbit = u16(ext, EXT_ROVINGBIT) % 32
            self._curranseqnr = u16(ext, EXT_CURRANSEQNR)
        else:
            self._res_roving = self._rovingbit = self._curranseqnr = 0
        self._roving_before = (self._res_roving, self._rovingbit, self._curranseqnr)

    def _require_writable(self) -> None:
        if not self.writable:
            raise DataError("This volume is open read-only.")
        if self.read_only_reason:
            raise DataError(self.read_only_reason)

    @contextmanager
    def _change(self):
        """Run one public change as a single copy-on-write update."""
        self._require_writable()
        if self._depth == 0:
            self._begin()
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self._abandon()
            raise
        self._depth -= 1
        if self._depth == 0:
            self._commit()

    def _begin(self) -> None:
        self._root_before = bytes(self._root)
        self._roving_before = (self._res_roving, self._rovingbit, self._curranseqnr)

    def _abandon(self) -> None:
        """Forget a change that failed before anything of it was committed."""
        if self._root_before is not None:
            self._root[:] = self._root_before
        self._res_roving, self._rovingbit, self._curranseqnr = self._roving_before
        self._dirty.clear()
        self._fresh.clear()
        self._data_frees.clear()
        self._reserved_frees.clear()
        self._full_anode_blocks.clear()
        self._max_anode_seq = None
        self._root_before = None

    def _checkpoint(self) -> None:
        """Commit a long change so far, at a point where it is consistent."""
        if self._depth and len(self._dirty) >= CHECKPOINT_BLOCKS:
            self._commit()
            self._begin()

    def _edit(self, number: int) -> bytearray:
        """Return a reserved block's contents for changing in this update."""
        data = self._dirty.get(number)
        if data is None:
            data = bytearray(self._reserved(number))
            self._dirty[number] = data
        return data

    def _ext_number(self) -> int:
        return u32(self._root, ROOT_EXTENSION) if self.options & MODE_EXTENSION else 0

    # ---- reserved area --------------------------------------------------
    def _alloc_reserved(self) -> int:
        free = u32(self._root, ROOT_RESERVED_FREE)
        if free <= RESFREE_THRESHOLD:
            raise DataError(
                "The PFS3 reserved area is full: the volume has no room left for "
                "directories or file records."
            )
        origin = RESERVED_BITMAP * 8
        start = self._res_roving if self._res_roving < self.num_reserved else 0
        bit = first_set_bit(self._root, origin + start, origin + self.num_reserved)
        if bit < 0:
            bit = first_set_bit(self._root, origin, origin + start)
        if bit < 0:
            raise DataError("The PFS3 reserved bitmap has no free block although its count says so.")
        index = bit - origin
        set_bits(self._root, bit, 1, False)
        put_u32(self._root, ROOT_RESERVED_FREE, free - 1)
        self._res_roving = index
        return self.firstreserved + index * self.rescluster

    def _release_reserved(self, number: int) -> None:
        index = (number - self.firstreserved) // self.rescluster
        if not 0 <= index < self.num_reserved:
            return
        bit = RESERVED_BITMAP * 8 + index
        if self._root[bit // 8] & (0x80 >> (bit % 8)):
            return
        set_bits(self._root, bit, 1, True)
        put_u32(self._root, ROOT_RESERVED_FREE, u32(self._root, ROOT_RESERVED_FREE) + 1)

    def _new_block(self, block_id: bytes, seqnr: int = 0) -> tuple[int, bytearray]:
        number = self._alloc_reserved()
        data = bytearray(self.reserved_blksize)
        data[:2] = block_id
        if block_id != DIRBLOCK_ID:
            put_u32(data, 8, seqnr)
        self._dirty[number] = data
        self._fresh.add(number)
        return number, data

    def _drop_block(self, number: int) -> None:
        """Release a reserved block the change no longer needs."""
        self._dirty.pop(number, None)
        self._fresh.discard(number)
        self._reserved_frees.append(number)

    # ---- anodes ---------------------------------------------------------
    def _create_index_block(self, index_nr: int) -> int:
        if self.supermode:
            super_nr, slot = divmod(index_nr, self.index_per_block)
            if super_nr > MAXSUPER:
                raise DataError("The PFS3 anode index is full.")
            superblock = self._superblock_number(super_nr)
            if not superblock:
                superblock, _data = self._new_block(SUPERBLOCK_ID, super_nr)
                put_u32(self._edit(self._ext_number()), EXT_SUPERINDEX + 4 * super_nr, superblock)
            number, _data = self._new_block(INDEXBLOCK_ID, index_nr)
            put_u32(self._edit(superblock), BLOCK_HEADER + 4 * slot, number)
            return number
        if index_nr > MAXSMALLINDEXNR:
            raise DataError("The PFS3 anode index is full.")
        number, _data = self._new_block(INDEXBLOCK_ID, index_nr)
        put_u32(self._root, ROOT_SMALL_INDEX + 4 * index_nr, number)
        return number

    def _create_anode_block(self, seqnr: int) -> int:
        index_nr, slot = divmod(seqnr, self.index_per_block)
        index = self._index_block_number(index_nr) or self._create_index_block(index_nr)
        number, _data = self._new_block(ANODEBLOCK_ID, seqnr)
        put_u32(self._edit(index), BLOCK_HEADER + 4 * slot, number)
        if self._max_anode_seq is not None and seqnr > self._max_anode_seq:
            self._max_anode_seq = seqnr
        return number

    def _highest_anode_seq(self) -> int:
        """The sequence number of the last anode block in the index, as the
        handler works it out when it mounts a volume."""
        if self._max_anode_seq is not None:
            return self._max_anode_seq
        per = self.index_per_block
        if self.supermode:
            index_blocks = []
            for super_nr in range(MAXSUPER + 1):
                superblock = self._superblock_number(super_nr)
                if superblock:
                    data = self._typed(superblock, SUPERBLOCK_ID, "super block")
                    index_blocks += [
                        (super_nr * per + slot, u32(data, BLOCK_HEADER + 4 * slot)) for slot in range(per)
                    ]
        else:
            index_blocks = [
                (index_nr, u32(self._root, ROOT_SMALL_INDEX + 4 * index_nr))
                for index_nr in range(MAXSMALLINDEXNR + 1)
            ]
        highest = -1
        for index_nr, number in reversed(index_blocks):
            if not number:
                continue
            data = self._typed(number, INDEXBLOCK_ID, "anode index")
            for slot in range(per - 1, -1, -1):
                if u32(data, BLOCK_HEADER + 4 * slot):
                    highest = index_nr * per + slot
                    break
            if highest >= 0:
                break
        self._max_anode_seq = highest
        return highest

    def _put_anode(self, number: int, clustersize: int, blocknr: int, following: int) -> None:
        seqnr, slot = self._split(number)
        block = self._anode_block_number(seqnr)
        if not block:
            raise DataError(f"PFS3 anode {number:#x} lies in an anode block that does not exist.")
        self._typed(block, ANODEBLOCK_ID, "anode block")
        pack_anode(self._edit(block), ANODEBLOCK_HEADER + slot * ANODE_SIZE, clustersize, blocknr, following)

    def _take_anode(self, seqnr: int, slot: int) -> int:
        number = self._join(seqnr, slot)
        self._put_anode(number, 0, EMPTY_BLOCKNR, 0)
        self._curranseqnr = seqnr & 0xFFFF
        return number

    def _free_slot(self, block: int, slots) -> int:
        data = self._typed(block, ANODEBLOCK_ID, "anode block")
        for slot in slots:
            offset = ANODEBLOCK_HEADER + slot * ANODE_SIZE
            if data[offset : offset + ANODE_SIZE] == _FREE_ANODE:
                return slot
        return -1

    def _alloc_anode(self, connect: int = 0) -> int:
        """Allocate an anode, next to ``connect`` when it has room."""
        per = self.anodes_per_block
        if connect and self.split_anodes:
            seqnr = connect >> 16
            block = self._anode_block_number(seqnr)
            if block:
                slot = self._free_slot(block, range(per - 1, -1, -1))
                if slot >= 0:
                    return self._take_anode(seqnr, slot)
        usable = range(per - RESERVEDANODES)
        highest = self._highest_anode_seq()
        start = min(self._curranseqnr, highest + 1)
        for seqnr in list(range(start, highest + 1)) + list(range(0, start)):
            if seqnr in self._full_anode_blocks:
                continue
            block = self._anode_block_number(seqnr)
            if not block:
                self._create_anode_block(seqnr)
                return self._take_anode(seqnr, 0)
            slot = self._free_slot(block, usable)
            if slot >= 0:
                return self._take_anode(seqnr, slot)
            self._full_anode_blocks.add(seqnr)
        seqnr = highest + 1
        if self.split_anodes and seqnr > 0xFFFF:
            raise DataError("The PFS3 anode index is full.")
        self._create_anode_block(seqnr)
        return self._take_anode(seqnr, 0)

    def _free_anode(self, number: int) -> None:
        if number < ANODE_USERFIRST:
            self._put_anode(number, 0, EMPTY_BLOCKNR, 0)
        else:
            self._put_anode(number, 0, 0, 0)
        self._full_anode_blocks.discard(self._split(number)[0])

    def _write_chain(self, head: int, runs: list[tuple[int, int]]) -> None:
        if not runs:
            self._put_anode(head, 0, EMPTY_BLOCKNR, 0)
            return
        numbers = [head]
        for _run in runs[1:]:
            numbers.append(self._alloc_anode(numbers[-1]))
        for index, (first, count) in enumerate(runs):
            following = numbers[index + 1] if index + 1 < len(numbers) else 0
            self._put_anode(numbers[index], count, first, following)

    def _free_chain(self, head: int, *, keep_head: bool) -> None:
        chain = self.anode_chain(head)
        for node in chain:
            if node.clustersize and node.blocknr not in (0, EMPTY_BLOCKNR):
                self._data_frees.append((node.blocknr, node.clustersize))
        for node in chain[1:]:
            self._free_anode(node.number)
        if keep_head:
            self._put_anode(head, 0, EMPTY_BLOCKNR, 0)
        else:
            self._free_anode(head)

    # ---- data blocks ----------------------------------------------------
    def _allocate_data(self, count: int) -> list[tuple[int, int]]:
        """Take ``count`` data blocks from the bitmap, from the roving pointer on."""
        if count <= 0:
            return []
        free = u32(self._root, ROOT_BLOCKSFREE)
        available = free - u32(self._root, ROOT_ALWAYSFREE)
        if count > available:
            raise DataError(
                f"{count:,} blocks are needed but the PFS3 volume has only "
                f"{max(available, 0):,} free."
            )
        per = self.bits_per_bitmap
        data_bits = self.total_blocks - self.bitmap_start
        pages = self.bitmap_blocks_needed
        position = u32(self._root, ROOT_ROVING_PTR) * 32 + self._rovingbit
        if position >= data_bits:
            position = 0
        first_page = position // per
        origin = BLOCK_HEADER * 8
        runs: list[tuple[int, int]] = []
        remaining = count
        after = position
        page, offset = first_page, position - first_page * per
        for step in range(pages + 1):
            covered = min(per, data_bits - page * per)
            stop = covered if step < pages else position - first_page * per
            if offset < stop:
                number = self._bitmap_block_number(page)
                if not number:
                    raise DataError(f"PFS3 bitmap block {page} is missing from its index.")
                data = self._typed(number, BITMAPBLOCK_ID, "bitmap block")
                bit = first_set_bit(data, origin + offset, origin + stop)
                while bit >= 0 and remaining:
                    end = first_clear_bit(data, bit, origin + stop)
                    take = min(end - bit, remaining)
                    if number not in self._dirty:
                        data = self._edit(number)
                    set_bits(data, bit, take, False)
                    block = self.bitmap_start + page * per + bit - origin
                    if runs and runs[-1][0] + runs[-1][1] == block:
                        runs[-1] = (runs[-1][0], runs[-1][1] + take)
                    else:
                        runs.append((block, take))
                    remaining -= take
                    after = page * per + bit - origin + take
                    bit = first_set_bit(data, bit + take, origin + stop)
            if not remaining:
                break
            page, offset = (page + 1) % pages, 0
        if remaining:
            raise DataError("The PFS3 bitmap holds fewer free blocks than the root block records.")
        put_u32(self._root, ROOT_BLOCKSFREE, free - count)
        if after >= data_bits:
            after = 0
        put_u32(self._root, ROOT_ROVING_PTR, after // 32)
        self._rovingbit = after % 32
        return runs

    def _apply_data_frees(self) -> None:
        per = self.bits_per_bitmap
        origin = BLOCK_HEADER * 8
        freed = 0
        for first, count in self._data_frees:
            start = max(first, self.bitmap_start)
            end = min(first + count, self.total_blocks)
            bit = start - self.bitmap_start
            remaining = end - start
            while remaining > 0:
                page, offset = divmod(bit, per)
                span = min(remaining, per - offset)
                number = self._bitmap_block_number(page)
                if number:
                    data = self._edit(number)
                    # Only blocks marked in use are counted back, so a block
                    # listed twice cannot inflate the free count.
                    freed += span - count_set_bits(data, origin + offset, origin + offset + span)
                    set_bits(data, origin + offset, span, True)
                bit += span
                remaining -= span
        self._data_frees.clear()
        if freed:
            put_u32(self._root, ROOT_BLOCKSFREE, u32(self._root, ROOT_BLOCKSFREE) + freed)

    def _write_data(self, runs: list[tuple[int, int]], payload: bytes) -> None:
        view = memoryview(payload)
        offset = 0
        for first, count in runs:
            length = count * self.block_size
            position = 0
            while position < length:
                piece = min(WRITE_CHUNK, length - position)
                chunk = bytes(view[offset + position : offset + position + piece])
                if len(chunk) < piece:
                    chunk = chunk.ljust(piece, b"\0")
                self.blocks.write_range(first * self.block_size + position, chunk)
                position += piece
            offset += length

    # ---- commit ---------------------------------------------------------
    def _relocate(self, number: int, kind: bytes) -> None:
        """Move one changed block to a new place and repoint its parent."""
        data = self._dirty.pop(number)
        moved = self._alloc_reserved()
        self._dirty[moved] = data
        self._fresh.add(moved)
        self._reserved_frees.append(number)
        seqnr = u32(data, 8)
        if kind == DIRBLOCK_ID:
            for node in self.anode_chain(u32(data, 12)):
                if node.blocknr == number:
                    self._put_anode(node.number, node.clustersize, moved, node.next)
                    return
            raise DataError(f"PFS3 directory block {number} is not in its directory's chain.")
        if kind == ANODEBLOCK_ID:
            index_nr, slot = divmod(seqnr, self.index_per_block)
            put_u32(self._edit(self._index_block_number(index_nr)), BLOCK_HEADER + 4 * slot, moved)
        elif kind == INDEXBLOCK_ID:
            if self.supermode:
                super_nr, slot = divmod(seqnr, self.index_per_block)
                put_u32(self._edit(self._superblock_number(super_nr)), BLOCK_HEADER + 4 * slot, moved)
            else:
                put_u32(self._root, ROOT_SMALL_INDEX + 4 * seqnr, moved)
        elif kind == SUPERBLOCK_ID:
            put_u32(self._edit(self._ext_number()), EXT_SUPERINDEX + 4 * seqnr, moved)
        elif kind == DELDIR_ID:
            if self.options & MODE_SUPERDELDIR:
                put_u32(self._edit(self._ext_number()), EXT_DELDIR + 4 * seqnr, moved)
            else:
                put_u32(self._root, ROOT_DELDIR, moved)
        elif kind == BITMAPBLOCK_ID:
            index_nr, slot = divmod(seqnr, self.index_per_block)
            put_u32(self._edit(self._bitmap_index_number(index_nr)), BLOCK_HEADER + 4 * slot, moved)
        elif kind == BITMAPINDEX_ID:
            put_u32(self._root, ROOT_BITMAPINDEX + 4 * seqnr, moved)
        elif kind == EXTENSION_ID:
            put_u32(self._root, ROOT_EXTENSION, moved)

    def _commit(self) -> None:
        changed = (
            self._dirty or self._data_frees or self._reserved_frees
            or (self._root_before is not None and bytes(self._root) != self._root_before)
        )
        if not changed:
            self._root_before = None
            return
        size = self.block_size
        try:
            self.blocks.sync()
            self._apply_data_frees()
            extension = self._ext_number()
            if extension:
                self._edit(extension)
            for kind in RELOCATION_ORDER:
                waiting = [
                    number for number, data in self._dirty.items()
                    if data[:2] == kind and number not in self._fresh
                ]
                for number in waiting:
                    self._relocate(number, kind)
            stamp = (u32(self._root, ROOT_DATESTAMP) + 1) & 0xFFFFFFFF
            put_u32(self._root, ROOT_DATESTAMP, stamp)
            extension = self._ext_number()
            if extension:
                ext = self._dirty[extension]
                roving = self._res_roving if self.options & MODE_EXTROVING else self._res_roving // 32
                put_u32(ext, EXT_RESERVED_ROVING, roving)
                put_u16(ext, EXT_ROVINGBIT, self._rovingbit)
                put_u16(ext, EXT_CURRANSEQNR, self._curranseqnr)
                for index, value in enumerate(datetime_to_triple(None)):
                    put_u16(ext, EXT_VOLUME_DATE + 2 * index, value)
            for number in sorted(self._dirty):
                data = self._dirty[number]
                put_u32(data, EXT_DATESTAMP if data[:2] == EXTENSION_ID else 4, stamp)
                self.blocks.write_range(number * size, bytes(data))
            self.blocks.sync()
            self.blocks.write_range(ROOTBLOCK * size, bytes(self._root))
            self.blocks.sync()
            if self._reserved_frees:
                for number in set(self._reserved_frees):
                    self._release_reserved(number)
                self.blocks.write_range(ROOTBLOCK * size, bytes(self._root))
                self.blocks.sync()
        except BaseException:
            self._reload()
            raise
        for number in self._reserved_frees:
            self._cache.pop(number, None)
        for number, data in self._dirty.items():
            if len(self._cache) >= self.CACHE_BLOCKS:
                self._cache.pop(next(iter(self._cache)))
            self._cache[number] = bytes(data)
        extension = self._ext_number()
        if extension:
            self._ext_committed = self._cache[extension] if extension in self._cache else self._reserved(extension)
        self._dirty.clear()
        self._fresh.clear()
        self._reserved_frees.clear()
        self._root_before = None
        self._roving_before = (self._res_roving, self._rovingbit, self._curranseqnr)

    def _reload(self) -> None:
        """Start again from what is on the disk after a commit went wrong."""
        depth = self._depth
        self._cache.clear()
        self._load_root()
        self._reset_changes()
        self._depth = depth

    # ---- directory entries ----------------------------------------------
    def _add_entry(self, main: int, raw: bytes) -> tuple[int, int]:
        """Put an entry in the first directory block with room for it.

        A full directory gets a new block at the front of its chain, as the
        handler does: the head anode is copied to a new anode next to it and
        then pointed at the new block, so the directory keeps its number.
        """
        size = self.reserved_blksize
        chain = self.anode_chain(main)
        for node in chain:
            data = self._typed(node.blocknr, DIRBLOCK_ID, "directory block")
            end = entries_end(data)
            if end + len(raw) + 1 < size:
                edit = self._edit(node.blocknr)
                edit[end : end + len(raw)] = raw
                edit[end + len(raw)] = 0
                return node.blocknr, end
        head = chain[0]
        parent = u32(self._typed(head.blocknr, DIRBLOCK_ID, "directory block"), 16)
        number, block = self._new_block(DIRBLOCK_ID)
        put_u32(block, 12, main)
        put_u32(block, 16, parent)
        block[DIRBLOCK_HEADER : DIRBLOCK_HEADER + len(raw)] = raw
        moved = self._alloc_anode(head.next or main)
        self._put_anode(moved, head.clustersize, head.blocknr, head.next)
        self._put_anode(main, 1, number, moved)
        return number, DIRBLOCK_HEADER

    def _remove_entry(self, entry: DirEntry) -> None:
        data = self._edit(entry.block)
        length = data[entry.offset]
        if length != entry.length or u32(data, entry.offset + 2) != entry.anode:
            raise DataError("A PFS3 directory entry moved while it was being changed.")
        size = len(data)
        data[entry.offset : size - length] = data[entry.offset + length : size]
        data[size - length :] = bytes(length)
        if not data[DIRBLOCK_HEADER]:
            self._drop_empty_dirblock(entry.directory, entry.block)

    def _drop_empty_dirblock(self, main: int, block: int) -> None:
        """Unlink an emptied directory block, unless it is the directory's first."""
        chain = self.anode_chain(main)
        if chain[0].blocknr == block:
            return
        for index in range(1, len(chain)):
            if chain[index].blocknr == block:
                previous = chain[index - 1]
                self._put_anode(previous.number, previous.clustersize, previous.blocknr, chain[index].next)
                self._free_anode(chain[index].number)
                self._drop_block(block)
                return

    def _replace_entry(self, entry: DirEntry, raw: bytes) -> tuple[int, int]:
        """Rewrite an entry, in place when it still fits its block."""
        data = self._reserved(entry.block)
        end = entries_end(data)
        grown_end = end - entry.length + len(raw)
        if grown_end < len(data):
            edit = self._edit(entry.block)
            tail = bytes(edit[entry.offset + entry.length : end])
            edit[entry.offset : entry.offset + len(raw)] = raw
            edit[entry.offset + len(raw) : grown_end] = tail
            edit[grown_end : max(end, grown_end + 1)] = bytes(max(end, grown_end + 1) - grown_end)
            return entry.block, entry.offset
        self._remove_entry(entry)
        return self._add_entry(entry.directory, raw)

    def _parent_of(self, main: int) -> int:
        head = self.anode(main)
        return u32(self._typed(head.blocknr, DIRBLOCK_ID, "directory block"), 16)

    def _touch(self, main: int, moment: datetime | None = None) -> None:
        """Date a directory whose contents changed, and clear its archive bit."""
        days, mins, ticks = datetime_to_triple(moment)
        if main == ANODE_ROOTDIR:
            extension = self._ext_number()
            if extension:
                ext = self._edit(extension)
                for index, value in enumerate((days, mins, ticks)):
                    put_u16(ext, EXT_ROOT_DATE + 2 * index, value)
            return
        entry = self.find_by_anode(self._parent_of(main), main)
        if entry is None or entry.type != ST_USERDIR:
            return
        data = self._edit(entry.block)
        put_u16(data, entry.offset + 10, days)
        put_u16(data, entry.offset + 12, mins)
        put_u16(data, entry.offset + 14, ticks)
        data[entry.offset + 16] &= ~FIBF_ARCHIVE & 0xFF

    def _encode(self, **fields) -> bytes:
        return encode_entry(dir_extension=self.dir_extension, largefile=self.largefile, **fields)

    def _reencode(self, entry: DirEntry, **changes) -> bytes:
        return reencode(entry, dir_extension=self.dir_extension, largefile=self.largefile, **changes)

    def _split_parent(self, path: str) -> tuple[DirEntry, bytes]:
        parts = split_path(path)
        if not parts:
            raise DataError("The volume root cannot be replaced.")
        parent, _ = self.resolve(join_path(parts[:-1]))
        if not parent.is_dir:
            raise DataError(f"{join_path(parts[:-1])} is not a directory.")
        return parent, encode_name(parts[-1], self.fnsize - 1)

    # ---- hard links -----------------------------------------------------
    def _link_nodes(self, first: int):
        seen: set[int] = set()
        number = first
        while number:
            if number in seen:
                raise DataError("A PFS3 hard link chain loops back on itself.")
            seen.add(number)
            node = self.anode(number)
            yield node
            number = node.next

    def _update_link_sizes(self, first: int, size: int) -> None:
        for node in list(self._link_nodes(first)):
            link = self.find_by_anode(node.blocknr, node.number)
            if link is not None:
                self._replace_entry(link, self._reencode(link, size=size))

    def _delete_link(self, link: DirEntry) -> None:
        node = self.anode(link.anode)
        target = self.find_by_anode(node.clustersize, link.extra.link)
        if target is not None:
            if target.extra.link == link.anode:
                self._replace_entry(target, self._reencode(target, extra=replace(target.extra, link=node.next)))
            else:
                for previous in list(self._link_nodes(target.extra.link)):
                    if previous.next == link.anode:
                        self._put_anode(previous.number, previous.clustersize, previous.blocknr, node.next)
                        break
        self._free_anode(link.anode)
        current = self.find_by_anode(link.directory, link.anode)
        if current is not None:
            self._remove_entry(current)
        self._touch(link.directory)

    def _promote_link(self, target: DirEntry) -> bool:
        """Delete an object that has hard links by making its first link the object.

        This is what the handler does: the first link's entry takes over the
        object's anode, type and size, its link node is freed, the remaining
        links are told the object now lives in that link's directory, and
        the object's own entry goes. Nothing of the data moves.
        """
        number = target.extra.link
        link = node = None
        while number:
            node = self.anode(number)
            link = self.find_by_anode(node.blocknr, node.number)
            if link is not None:
                break
            # A link node whose entry has gone is simply discarded.
            self._free_anode(node.number)
            number = node.next
        if link is None:
            return False
        raw = self._reencode(
            link, entry_type=target.type, anode=target.anode, size=target.size,
            extra=replace(link.extra, link=node.next),
        )
        self._free_anode(node.number)
        self._remove_entry(self.find_by_anode(target.directory, target.anode))
        self._touch(target.directory)
        link = self.find_by_anode(node.blocknr, node.number)
        self._replace_entry(link, raw)
        for following in list(self._link_nodes(node.next)):
            self._put_anode(following.number, node.blocknr, following.blocknr, following.next)
        if target.type == ST_USERDIR:
            for block in self.anode_chain(target.anode):
                put_u32(self._edit(block.blocknr), 16, node.blocknr)
        return True

    # ---- public changes -------------------------------------------------
    def write_bytes(self, path: str, data: bytes, meta: AmigaMeta | None = None) -> int:
        """Create or replace a file, returning its anode number."""
        with self._change():
            parent, raw_name = self._split_parent(path)
            main = parent.listing_anode
            payload = bytes(data)
            comment = encode_comment(meta.comment) if meta is not None else None
            existing = self.find(main, raw_name.decode("latin-1"))
            if existing is not None:
                if existing.is_dir:
                    raise DataError(f"{path} is a directory.")
                if existing.type != ST_FILE:
                    raise DataError(f"{path} is a link or a rollover file; replace it on the Amiga.")
                if existing.protection & FIBF_DELETE:
                    raise DataError(f"{path} is protected from deletion.")
            runs = self._allocate_data(math.ceil(len(payload) / self.block_size))
            self._write_data(runs, payload)
            if existing is not None:
                anode = existing.anode
                self._free_chain(anode, keep_head=True)
            else:
                anode = self._alloc_anode()
            self._write_chain(anode, runs)
            if meta is not None:
                protection = int(meta.protection)
            elif existing is not None:
                protection = existing.full_protection
            else:
                protection = DEFAULT_PROTECTION
            if comment is None:
                comment = existing.comment.encode("latin-1") if existing is not None else b""
            days, mins, ticks = datetime_to_triple(meta.datestamp if meta is not None else None)
            extra = replace(existing.extra, virtualsize=0, rollpointer=0) if existing else ExtraFields()
            raw = self._encode(
                raw_name=raw_name, entry_type=ST_FILE, anode=anode, size=len(payload),
                days=days, mins=mins, ticks=ticks, protection=protection, comment=comment,
                extra=extra,
            )
            if existing is not None:
                self._replace_entry(existing, raw)
                if extra.link:
                    self._update_link_sizes(extra.link, len(payload))
            else:
                self._add_entry(main, raw)
            self._touch(main)
            return anode

    def mkdir(self, path: str) -> int:
        with self._change():
            parent, raw_name = self._split_parent(path)
            main = parent.listing_anode
            if self.find(main, raw_name.decode("latin-1")) is not None:
                raise DataError(f"{path} already exists.")
            anode = self._alloc_anode()
            days, mins, ticks = datetime_to_triple(None)
            self._add_entry(main, self._encode(
                raw_name=raw_name, entry_type=ST_USERDIR, anode=anode, size=0,
                days=days, mins=mins, ticks=ticks, protection=DEFAULT_PROTECTION,
                comment=b"", extra=ExtraFields(),
            ))
            number, block = self._new_block(DIRBLOCK_ID)
            put_u32(block, 12, anode)
            put_u32(block, 16, main)
            self._put_anode(anode, 1, number, 0)
            self._touch(main)
            return anode

    def _delete(self, entry: DirEntry, *, recursive: bool) -> None:
        if entry.is_link:
            self._delete_link(entry)
            return
        if entry.extra.link and self._promote_link(entry):
            return
        if entry.type == ST_USERDIR:
            while True:
                child = next(iter(self.entries(entry.anode)), None)
                if child is None:
                    break
                if not recursive:
                    raise DataError(f"{entry.name} is not empty.")
                self._delete(child, recursive=True)
                self._checkpoint()
            for node in self.anode_chain(entry.anode):
                self._free_anode(node.number)
                self._drop_block(node.blocknr)
        else:
            self._free_chain(entry.anode, keep_head=False)
        current = self.find_by_anode(entry.directory, entry.anode)
        if current is None:
            raise DataError(f"{entry.name} is no longer in its directory.")
        self._remove_entry(current)
        self._touch(entry.directory)

    def remove(self, path: str, *, recursive: bool = False) -> None:
        with self._change():
            found, parts = self.resolve(path)
            if not parts:
                raise DataError("The volume root cannot be deleted.")
            if not found.is_dir and found.protection & FIBF_DELETE:
                raise DataError(f"{path} is protected from deletion.")
            self._delete(found, recursive=recursive)

    def rename(self, source: str, destination: str) -> None:
        with self._change():
            found, parts = self.resolve(source)
            if not parts:
                raise DataError("The volume root cannot be moved.")
            parent, raw_name = self._split_parent(destination)
            target = parent.listing_anode
            origin = found.directory
            clash = self.find(target, raw_name.decode("latin-1"))
            if clash is not None and not (clash.anode == found.anode and clash.directory == origin):
                raise DataError(f"{destination} already exists.")
            if found.type == ST_USERDIR and target != origin:
                ancestor = target
                while ancestor:
                    if ancestor == found.anode:
                        raise DataError("A directory cannot be moved inside itself.")
                    if ancestor == ANODE_ROOTDIR:
                        break
                    ancestor = self._parent_of(ancestor)
            raw = self._reencode(found, raw_name=raw_name)
            if target == origin:
                self._replace_entry(found, raw)
                self._touch(origin)
                return
            self._add_entry(target, raw)
            current = self.find_by_anode(origin, found.anode)
            self._remove_entry(current)
            if found.type == ST_USERDIR:
                for node in self.anode_chain(found.anode):
                    put_u32(self._edit(node.blocknr), 16, target)
            if found.is_link:
                node = self.anode(found.anode)
                self._put_anode(found.anode, node.clustersize, target, node.next)
            elif found.extra.link:
                for node in list(self._link_nodes(found.extra.link)):
                    self._put_anode(node.number, target, node.blocknr, node.next)
            self._touch(origin)
            self._touch(target)

    def set_amiga_meta(self, path: str, meta: AmigaMeta) -> None:
        with self._change():
            found, parts = self.resolve(path)
            comment = encode_comment(meta.comment)
            if not parts:
                if meta.datestamp is not None:
                    self._touch(ANODE_ROOTDIR, meta.datestamp)
                return
            days, mins, ticks = (
                datetime_to_triple(meta.datestamp) if meta.datestamp is not None
                else (found.days, found.mins, found.ticks)
            )
            self._replace_entry(found, self._reencode(
                found, protection=int(meta.protection), comment=comment,
                days=days, mins=mins, ticks=ticks,
            ))

    def set_access(self, path: str, access) -> None:
        value = access.value if hasattr(access, "value") else int(access)
        self.set_amiga_meta(path, self.amiga_meta(path).with_protection(value))

    def set_comment(self, path: str, value: str) -> None:
        self.set_amiga_meta(path, self.amiga_meta(path).with_comment(value))

    def set_datestamp(self, path: str, moment: datetime) -> None:
        meta = self.amiga_meta(path)
        self.set_amiga_meta(path, AmigaMeta(protection=meta.protection, comment=meta.comment, datestamp=moment))

    def set_title(self, value: str) -> None:
        with self._change():
            raw = encode_name(value, MAX_DISKNAME, "volume name")
            self._root[ROOT_DISKNAME : ROOT_DISKNAME + 32] = bytes([len(raw)]) + raw.ljust(31, b"\0")

    def set_boot_option(self, option: int) -> None:
        raise DataError(
            "PFS3 has no boot block options. Whether a partition boots is set in "
            "the drive's partition table."
        )

    def defragment(self) -> int:
        raise DataError(
            "Defragmenting is not offered for PFS3 volumes. PFS3 allocates files in "
            "long extents and has its own tools on the Amiga for this."
        )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def format_pfs3_volume(
    reader: BlockReader,
    *,
    label: str = "Empty",
    block_size: int = 512,
    name_length: int = DEFAULT_FNSIZE - 1,
    deldir_blocks: int = 2,
) -> None:
    """Write a new, empty PFS3 volume across the whole of ``reader``.

    The layout is the one pfs3aio's own format writes: the boot block, the
    root cluster with its reserved bitmap, the root block extension straight
    after it, then the bitmap index and bitmap blocks, the anode index, the
    first anode block with the five reserved anodes and the root directory's,
    the root directory block and the deleted-files directory blocks, all
    allocated from the start of the reserved area. Volumes larger than about
    5 GB use super index mode, and larger reserved blocks past 104 GB, as the
    handler would choose. ``name_length`` is the longest name the volume
    accepts, 31 as the handler formats unless raised, up to 106.
    """
    name = encode_name(label, MAX_DISKNAME, "volume name")
    # The volume is written through a handle of its own, at its own block
    # size. Anything the caller has written and not yet flushed would reach
    # the file after it, so it is flushed first.
    reader.flush()
    if block_size not in (512, 1024, 2048, 4096) or block_size < reader.block_size:
        raise DataError("The PFS3 block size must be 512, 1024, 2048 or 4096 bytes.")
    fnsize = int(name_length) + 1
    if not DEFAULT_FNSIZE <= fnsize <= MAX_FNSIZE:
        raise DataError(f"A PFS3 volume stores names of {DEFAULT_FNSIZE - 1} to {MAX_FNSIZE - 1} characters.")
    if not 0 <= deldir_blocks <= 32:
        raise DataError("A PFS3 deleted-files directory has at most 32 blocks.")
    sectors = reader.length // reader.block_size
    if sectors > MAXDISKSIZE4K:
        raise DataError("The partition is too large for PFS3, which stops at about 1.6 TB.")
    base = -(-reader.offset // block_size) * block_size
    end = (reader.offset + reader.length) // block_size * block_size
    blocks = reader.reopen(writable=True, offset=base, length=end - base, block_size=block_size)
    try:
        _format(blocks, name, sectors, fnsize, deldir_blocks)
    finally:
        blocks.close()


def _format(blocks: BlockReader, name: bytes, sectors: int, fnsize: int, deldir_blocks: int) -> None:
    size = blocks.block_size
    total = blocks.total_blocks
    options = (
        MODE_HARDDISK | MODE_SPLITTED_ANODES | MODE_DIR_EXTENSION | MODE_SIZEFIELD
        | MODE_DATESTAMP | MODE_EXTROVING | MODE_LONGFN | MODE_EXTENSION
    )
    reserved_size = 1024
    if sectors > MAXSMALLDISK:
        options |= MODE_SUPERINDEX
        if sectors > MAXDISKSIZE1K:
            reserved_size = 4096 if sectors > MAXDISKSIZE2K else 2048
    reserved_size = max(reserved_size, size)
    disktype = PFS2_ID if reserved_size > 1024 or size > 512 else PFS1_ID
    if disktype == PFS2_ID:
        options |= MODE_SUPERINDEX
    supermode = bool(options & MODE_SUPERINDEX)
    rescluster = reserved_size // size
    per_index = (reserved_size - BLOCK_HEADER) // 4
    longs_per_bitmap = reserved_size // 4 - 3
    num_reserved = calc_num_reserved(sectors, reserved_size)
    first_reserved = ROOTBLOCK
    last_reserved = rescluster * num_reserved + first_reserved - 1
    if last_reserved + 64 >= total:
        raise DataError("The partition is too small for a PFS3 volume.")
    blocksfree = total - rescluster * num_reserved - first_reserved
    cluster_blocks = root_cluster_blocks(num_reserved, reserved_size)
    root = bytearray(cluster_blocks * reserved_size)
    root[RESERVED_BITMAP - 12 : RESERVED_BITMAP - 10] = BITMAPBLOCK_ID
    set_bits(root, RESERVED_BITMAP * 8, num_reserved, True)
    set_bits(root, RESERVED_BITMAP * 8, cluster_blocks + 1, False)
    extension = first_reserved + cluster_blocks * rescluster
    state = {"roving": cluster_blocks, "free": num_reserved - cluster_blocks - 1}
    written: dict[int, bytearray] = {}

    def allocate(block_id: bytes, seqnr: int = 0) -> tuple[int, bytearray]:
        bit = first_set_bit(root, RESERVED_BITMAP * 8 + state["roving"], RESERVED_BITMAP * 8 + num_reserved)
        if bit < 0:
            raise DataError("The partition is too small for a PFS3 volume.")
        set_bits(root, bit, 1, False)
        index = bit - RESERVED_BITMAP * 8
        state["roving"] = index
        state["free"] -= 1
        number = first_reserved + index * rescluster
        data = bytearray(reserved_size)
        data[:2] = block_id
        put_u32(data, 4, 1)
        if block_id != DIRBLOCK_ID:
            put_u32(data, 8, seqnr)
        written[number] = data
        return number, data

    days, mins, ticks = datetime_to_triple(None)
    ext = bytearray(reserved_size)
    ext[:2] = EXTENSION_ID
    put_u32(ext, EXT_DATESTAMP, 1)
    put_u32(ext, EXT_VERSION, (VERNUM << 16) + REVNUM)
    for index, value in enumerate((days, mins, ticks)):
        put_u16(ext, EXT_ROOT_DATE + 2 * index, value)
        put_u16(ext, EXT_VOLUME_DATE + 2 * index, value)
    put_u16(ext, EXT_FNSIZE, fnsize)

    # The bitmap, every bit free; bits past the end of the volume stay set
    # as the handler leaves them, and are never looked at.
    longs = -(-(total - last_reserved - 1) // 32)
    bitmap_blocks = -(-longs // longs_per_bitmap)
    index_limit = MAXBITMAPINDEX if supermode else MAXSMALLBITMAPINDEX
    bitmap_index: list[bytearray] = []
    for seqnr in range(bitmap_blocks):
        index_nr, slot = divmod(seqnr, per_index)
        if index_nr > index_limit:
            raise DataError("The partition is too large for this PFS3 layout.")
        if index_nr == len(bitmap_index):
            number, data = allocate(BITMAPINDEX_ID, index_nr)
            put_u32(root, ROOT_BITMAPINDEX + 4 * index_nr, number)
            bitmap_index.append(data)
        number, data = allocate(BITMAPBLOCK_ID, seqnr)
        data[BLOCK_HEADER:] = b"\xff" * (reserved_size - BLOCK_HEADER)
        put_u32(bitmap_index[index_nr], BLOCK_HEADER + 4 * slot, number)
        blocks.write_range(number * size, bytes(data))
        del written[number]

    # The first anode block, reached through super block 0 on large volumes.
    if supermode:
        superblock, super_data = allocate(SUPERBLOCK_ID, 0)
        put_u32(ext, EXT_SUPERINDEX, superblock)
        index_block, index_data = allocate(INDEXBLOCK_ID, 0)
        put_u32(super_data, BLOCK_HEADER, index_block)
    else:
        index_block, index_data = allocate(INDEXBLOCK_ID, 0)
        put_u32(root, ROOT_SMALL_INDEX, index_block)
    anode_block, anodes = allocate(ANODEBLOCK_ID, 0)
    put_u32(index_data, BLOCK_HEADER, anode_block)
    for slot in range(ANODE_ROOTDIR):
        pack_anode(anodes, ANODEBLOCK_HEADER + slot * ANODE_SIZE, 0, EMPTY_BLOCKNR, 0)
    directory, dir_data = allocate(DIRBLOCK_ID)
    put_u32(dir_data, 12, ANODE_ROOTDIR)
    put_u32(dir_data, 16, 0)
    pack_anode(anodes, ANODEBLOCK_HEADER + ANODE_ROOTDIR * ANODE_SIZE, 1, directory, 0)

    if deldir_blocks:
        options |= MODE_DELDIR | MODE_SUPERDELDIR
        for seqnr in range(deldir_blocks):
            number, data = allocate(DELDIR_ID, seqnr)
            put_u32(data, 22, 0x0005)
            for index, value in enumerate((days, mins, ticks)):
                put_u16(data, 26 + 2 * index, value)
            put_u32(ext, EXT_DELDIR + 4 * seqnr, number)
        put_u16(ext, EXT_DELDIRSIZE, deldir_blocks)

    put_u32(ext, EXT_RESERVED_ROVING, state["roving"])

    root[:4] = disktype
    put_u32(root, ROOT_OPTIONS, options)
    put_u32(root, ROOT_DATESTAMP, 1)
    for index, value in enumerate((days, mins, ticks)):
        put_u16(root, ROOT_CREATION + 2 * index, value)
    put_u16(root, ROOT_PROTECTION, 0xF0)
    root[ROOT_DISKNAME] = len(name)
    root[ROOT_DISKNAME + 1 : ROOT_DISKNAME + 1 + len(name)] = name
    put_u32(root, ROOT_LASTRESERVED, last_reserved)
    put_u32(root, ROOT_FIRSTRESERVED, first_reserved)
    put_u32(root, ROOT_RESERVED_FREE, state["free"])
    put_u16(root, ROOT_RESERVED_BLKSIZE, reserved_size)
    put_u16(root, ROOT_RBLKCLUSTER, cluster_blocks * rescluster)
    put_u32(root, ROOT_BLOCKSFREE, blocksfree)
    put_u32(root, ROOT_ALWAYSFREE, blocksfree // 20)
    put_u32(root, ROOT_DISKSIZE, sectors)
    put_u32(root, ROOT_EXTENSION, extension)

    for number in sorted(written):
        blocks.write_range(number * size, bytes(written[number]))
    blocks.write_range(extension * size, bytes(ext))
    boot = bytearray(2 * size)
    boot[:4] = PFS1_ID
    blocks.write_range(0, bytes(boot))
    blocks.sync()
    blocks.write_range(ROOTBLOCK * size, bytes(root))
    blocks.sync()


__all__ = ["PFS3Writer", "format_pfs3_volume"]
