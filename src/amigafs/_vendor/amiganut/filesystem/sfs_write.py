"""Changing a Smart File System volume the way SFS itself would.

Every public change runs as one transaction. Metadata blocks are edited in
memory, and when the change is complete they are written with SFS's own
crash protocol:

1. File data has already been written, into blocks that are free both on the
   disk and in the pending change, and is flushed to the medium.
2. The new contents of every changed metadata block are written to free space
   as a transaction log (``TRST`` blocks), and flushed.
3. A ``TRFA`` marker is written two blocks after the root object container,
   pointing at the log, and flushed.
4. The metadata blocks are written in place, and flushed.
5. The marker goes back to ``TROK``.

Power lost before step 3 leaves the volume exactly as it was: nothing live has
been touched. Power lost after it leaves a log that SFS replays when the
volume is next mounted, on an Amiga or here. The log is written in the run
encoding of the SFS 1.x release build, against an all-zero original, so a
replay produces the same blocks whatever state they were left in.

Allocation follows SFS as well. Metadata blocks come from 32-block regions
recorded in the admin space containers; file data comes from the bitmap,
starting beyond the reserve SFS keeps for metadata near the start of the
volume. Blocks freed by a change are not reused for data until it commits,
because data is written before the change is, and the old contents must stay
intact until then.
"""

from __future__ import annotations

import math
import struct
from contextlib import contextmanager

from ..errors import DataError
from ..file import AmigaMeta, DEFAULT_PROTECTION
from .amigados import join_path, split_path
from .blocks import BlockReader
from .sfs_blocks import (
    ADMIN_HEADER,
    ADMIN_REGION,
    ADMINSPACECONTAINER_ID,
    ALWAYS_FREE,
    BITMAP_ID,
    BNODE_CONTAINER_HEADER,
    BNODE_SIZE,
    BNODECONTAINER_ID,
    CONTAINER_HEADER,
    EXTENT_NODE_SIZE,
    FIBF_ARCHIVE,
    HASHTABLE_ID,
    HEADER_SIZE,
    MAX_COMMENT_LENGTH,
    MAX_EXTENT_BLOCKS,
    NODE_CONTAINER_HEADER,
    NODECONTAINER_ID,
    OBJECT_HEADER,
    OBJECT_NODE_SIZE,
    OBJECT_TAIL,
    OBJECTCONTAINER_ID,
    STRUCTURE_VERSION,
    SFS_ID,
    ROOTBITS_RECYCLED,
    ROOTBITS_CASESENSITIVE,
    OTYPE_HIDDEN,
    OI_EMPTY,
    OTYPE_DIR,
    OTYPE_QUICKDIR,
    OTYPE_UNDELETABLE,
    PERMISSION_BITS,
    RECYCLED_NODE,
    ROOT_INFO_SIZE,
    ROOT_NODE,
    TRANSACTIONFAILURE_ID,
    TRANSACTIONOK_ID,
    TRANSACTIONSTORAGE_ID,
    SFSObject,
    compress_operation,
    datetime_to_sfs,
    encode_object,
    long_at,
    object_end,
    object_space,
    put_long,
    put_word,
    seal_sfs_block,
    sfs_hash,
    validate_sfs_name,
    verify_sfs_block,
    word_at,
)

#: File data is stamped with this many spare blocks of headroom for the
#: metadata and transaction log a change needs on top of its data.
CHANGE_HEADROOM = 64


class SFSWriter:
    """The change half of ``SFSVolume``; it relies on the reader's attributes."""

    # ---- transaction state ------------------------------------------
    def _reset_changes(self) -> None:
        self._pending: dict[int, bytearray] = {}
        self._depth = 0
        self._committed_pages: dict[int, bytes] = {}
        self._roving: int | None = None

    def _require_writable(self) -> None:
        if not self.writable:
            raise DataError("This volume is open read-only.")

    @contextmanager
    def _change(self):
        """Run one public change as a single SFS transaction."""
        self._require_writable()
        if self._depth == 0 and self.overlay:
            self._complete_recovered_transaction()
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self._pending.clear()
                self._committed_pages.clear()
            raise
        self._depth -= 1
        if self._depth == 0:
            self._commit()

    def _edit(self, number: int, block_id: bytes) -> bytearray:
        """Return a block's contents for changing within this transaction."""
        if number not in self._pending:
            self._pending[number] = bytearray(self.read_typed(number, block_id))
        elif self._pending[number][:4] != block_id:
            raise DataError(f"SFS block {number} is not {block_id.decode('latin-1').strip()}.")
        return self._pending[number]

    def _create(self, number: int, block_id: bytes) -> bytearray:
        """Start a brand-new metadata block in this transaction."""
        block = bytearray(self.block_size)
        block[:4] = block_id
        self._pending[number] = block
        return block

    def _complete_recovered_transaction(self) -> None:
        """Finish the change SFS left half written before making a new one."""
        for number, data in sorted(self.overlay.items()):
            self.blocks.write_block(number, data)
            self._remember(number, data)
        self.blocks.sync()
        self._write_marker(TRANSACTIONOK_ID)
        self.overlay.clear()
        self.recovered_transaction = False

    def _write_marker(self, block_id: bytes, first_log_block: int = 0) -> None:
        number = self.root_container + 2
        marker = bytearray(self.block_size)
        marker[:4] = block_id
        if first_log_block:
            put_long(marker, HEADER_SIZE, first_log_block)
        sealed = seal_sfs_block(marker, number)
        self.blocks.write_block(number, sealed)
        self._remember(number, sealed)
        self.blocks.sync()

    def _commit(self) -> None:
        """Write the pending metadata through the SFS transaction protocol."""
        try:
            if not self._pending:
                return
            sealed = {
                number: seal_sfs_block(bytearray(data), number)
                for number, data in sorted(self._pending.items())
            }
            self.blocks.sync()
            stream = bytearray()
            for number, data in sealed.items():
                payload = compress_operation(data)
                stream += struct.pack(">HIB", len(payload), number, OI_EMPTY) + payload
                if not len(payload) & 1:
                    stream += b"\0"
            stream += b"\0\0"
            per_block = self.block_size - HEADER_SIZE - 4
            log_blocks = self._log_space(math.ceil(len(stream) / per_block))
            for index, number in enumerate(log_blocks):
                storage = bytearray(self.block_size)
                storage[:4] = TRANSACTIONSTORAGE_ID
                following = log_blocks[index + 1] if index + 1 < len(log_blocks) else 0
                put_long(storage, HEADER_SIZE, following)
                chunk = stream[index * per_block : (index + 1) * per_block]
                storage[HEADER_SIZE + 4 : HEADER_SIZE + 4 + len(chunk)] = chunk
                self.blocks.write_block(number, seal_sfs_block(storage, number))
                self._forget(number)
            self.blocks.sync()
            self._write_marker(TRANSACTIONFAILURE_ID, log_blocks[0])
            for number, data in sealed.items():
                self.blocks.write_block(number, data)
                self._remember(number, data)
            self.blocks.sync()
            self._write_marker(TRANSACTIONOK_ID)
        finally:
            self._pending.clear()
            self._committed_pages.clear()

    # ---- the bitmap --------------------------------------------------
    @property
    def _bits_per_page(self) -> int:
        return (self.block_size - HEADER_SIZE) * 8

    def _page_value(self, page: int, *, committed: bool) -> int:
        """One bitmap block as an integer, most significant bit first."""
        number = self.bitmap_base + page
        if not committed and number in self._pending:
            return int.from_bytes(self._pending[number][HEADER_SIZE:], "big")
        if page not in self._committed_pages:
            data = self.overlay.get(number) or self.blocks.read_block(number)
            if not verify_sfs_block(data, number, BITMAP_ID):
                raise DataError(f"SFS bitmap block {number} is damaged.")
            self._committed_pages[page] = data
        return int.from_bytes(self._committed_pages[page][HEADER_SIZE:], "big")

    def _usable_page(self, page: int) -> str:
        """The page's blocks free both on disk and in this change, as '0'/'1'."""
        per_page = self._bits_per_page
        value = self._page_value(page, committed=False) & self._page_value(page, committed=True)
        text = format(value, f"0{per_page}b")
        covered = min(per_page, self.total_blocks - page * per_page)
        return text[:covered]

    def _free_runs(self, start: int, enough: int = 0):
        """Yield runs of usable blocks as (first, length), from ``start`` round to it.

        A run is normally given once its end is found. On a new volume of many
        gigabytes the first run is the whole of the free space, and finding
        its end means reading every bitmap block of the volume to place one
        file. With ``enough``, a run is given as soon as it is that long, and
        what follows it is given as another.
        """
        per_page = self._bits_per_page
        start %= self.total_blocks
        for segment_start, segment_end in ((start, self.total_blocks), (0, start)):
            run_start = None
            block = segment_start
            while block < segment_end:
                page = block // per_page
                base = page * per_page
                text = self._usable_page(page)
                position = block - base
                limit = min(len(text), segment_end - base)
                while position < limit:
                    if text[position] == "1":
                        stop = text.find("0", position, limit)
                        stop = limit if stop < 0 else stop
                        if run_start is None:
                            run_start = base + position
                        if stop < limit:
                            yield run_start, base + stop - run_start
                            run_start = None
                        position = stop
                    else:
                        if run_start is not None:
                            yield run_start, base + position - run_start
                            run_start = None
                        following = text.find("1", position, limit)
                        position = limit if following < 0 else following
                block = base + limit
                if enough and run_start is not None and block - run_start >= enough:
                    yield run_start, block - run_start
                    run_start = None
            if run_start is not None:
                yield run_start, segment_end - run_start

    def _find_space(self, wanted: int, start: int, *, contiguous: bool, limit: int | None = None):
        """Find ``wanted`` usable blocks, searching from ``start`` and wrapping.

        With ``contiguous`` the answer is one run of exactly ``wanted`` blocks.
        Otherwise it is as few runs as the free space allows, each split to at
        most ``limit`` blocks.
        """
        runs: list[tuple[int, int]] = []
        found = 0
        for first, length in self._free_runs(start, wanted):
            if contiguous:
                if length >= wanted:
                    return [(first, wanted)]
                continue
            take = min(length, wanted - found)
            while take:
                piece = min(take, limit or take)
                runs.append((first, piece))
                first += piece
                take -= piece
                found += piece
            if found >= wanted:
                return runs
        raise DataError("There is not enough free space on this SFS volume.")

    def _mark(self, first: int, count: int, *, used: bool) -> None:
        """Mark a range used or free, keeping the cached free count in step."""
        per_page = self._bits_per_page
        block, remaining = first, count
        while remaining:
            page, bit = divmod(block, per_page)
            length = min(remaining, per_page - bit)
            data = self._edit(self.bitmap_base + page, BITMAP_ID)
            value = int.from_bytes(data[HEADER_SIZE:], "big")
            mask = ((1 << length) - 1) << (per_page - bit - length)
            if used:
                if value & mask != mask:
                    raise DataError(f"Blocks from {block} are already in use.")
                value &= ~mask
            else:
                if value & mask:
                    raise DataError(f"Blocks from {block} are already free.")
                value |= mask
            data[HEADER_SIZE:] = value.to_bytes(per_page // 8, "big")
            block += length
            remaining -= length
        self._adjust_root_info(free=-count if used else count)

    def _adjust_root_info(self, *, free: int = 0, deleted_files: int = 0, deleted_blocks: int = 0) -> None:
        root = self._edit(self.root_container, OBJECTCONTAINER_ID)
        base = self.block_size - ROOT_INFO_SIZE
        for offset, delta in ((0, deleted_blocks), (4, deleted_files), (8, free)):
            if delta:
                put_long(root, base + offset, max(0, long_at(root, base + offset) + delta))

    def _free_blocks(self) -> int:
        root = self.read_typed(self.root_container, OBJECTCONTAINER_ID)
        return long_at(root, self.block_size - ROOT_INFO_SIZE + 8)

    def _roving_start(self) -> int:
        """Where SFS starts looking for file data: past its metadata reserve."""
        if self._roving is None:
            bitmap_blocks = math.ceil(self.total_blocks / self._bits_per_page)
            blocks512 = self.total_blocks * self.block_size // 512
            reserve = math.isqrt(blocks512) * 5
            reserve = min(reserve, blocks512 // 100)
            if reserve < ADMIN_REGION:
                reserve = ADMIN_REGION if ADMIN_REGION <= self.total_blocks // 2 else 0
            self._roving = self.admin_space + ADMIN_REGION + bitmap_blocks + reserve
        return self._roving % self.total_blocks

    def _log_space(self, count: int) -> list[int]:
        """Pick free blocks for the transaction log without claiming them."""
        runs = self._find_space(count, self._roving_start(), contiguous=False)
        blocks = [first + offset for first, length in runs for offset in range(length)]
        return [block for block in blocks if block not in self._pending][:count]

    def _allocate_data(self, count: int) -> list[tuple[int, int]]:
        if self._free_blocks() < count + ALWAYS_FREE + CHANGE_HEADROOM:
            raise DataError("There is not enough free space on this SFS volume.")
        runs = self._find_space(
            count, self._roving_start(), contiguous=False, limit=MAX_EXTENT_BLOCKS
        )
        for first, length in runs:
            self._mark(first, length, used=True)
        last_first, last_length = runs[-1]
        self._roving = last_first + last_length
        return runs

    # ---- admin space -------------------------------------------------
    def _admin_entries(self, data: bytes) -> int:
        return (self.block_size - ADMIN_HEADER) // 8

    def _allocate_admin(self, block_id: bytes) -> tuple[int, bytearray]:
        """Hand out one metadata block and start it with ``block_id``."""
        container = self.admin_space
        seen: set[int] = set()
        while container and container not in seen:
            seen.add(container)
            data = self.read_typed(container, ADMINSPACECONTAINER_ID)
            for index in range(self._admin_entries(data)):
                offset = ADMIN_HEADER + index * 8
                space, bits = long_at(data, offset), long_at(data, offset + 4)
                if space and bits != 0xFFFFFFFF:
                    bit = next(b for b in range(32) if not bits & (1 << (31 - b)))
                    edit = self._edit(container, ADMINSPACECONTAINER_ID)
                    put_long(edit, offset + 4, bits | (1 << (31 - bit)))
                    number = space + bit
                    return number, self._create(number, block_id)
            container = long_at(data, HEADER_SIZE)
        self._add_admin_region()
        return self._allocate_admin(block_id)

    def _add_admin_region(self) -> None:
        """Claim 32 more blocks for metadata and record them."""
        if self._free_blocks() < ADMIN_REGION + ALWAYS_FREE:
            raise DataError("There is not enough free space on this SFS volume.")
        (start, _count), = self._find_space(ADMIN_REGION, 0, contiguous=True)
        self._mark(start, ADMIN_REGION, used=True)
        container = self.admin_space
        while True:
            data = self.read_typed(container, ADMINSPACECONTAINER_ID)
            for index in range(self._admin_entries(data)):
                offset = ADMIN_HEADER + index * 8
                if not long_at(data, offset):
                    edit = self._edit(container, ADMINSPACECONTAINER_ID)
                    put_long(edit, offset, start)
                    put_long(edit, offset + 4, 0)
                    return
            following = long_at(data, HEADER_SIZE)
            if not following:
                break
            container = following
        # Every container is full: the new region's first block becomes the
        # next container, and records the region it sits in.
        edit = self._edit(container, ADMINSPACECONTAINER_ID)
        put_long(edit, HEADER_SIZE, start)
        fresh = self._create(start, ADMINSPACECONTAINER_ID)
        put_long(fresh, HEADER_SIZE + 4, container)
        fresh[HEADER_SIZE + 8] = ADMIN_REGION
        put_long(fresh, ADMIN_HEADER, start)
        put_long(fresh, ADMIN_HEADER + 4, 0x80000000)

    def _free_admin(self, number: int) -> None:
        container = self.admin_space
        seen: set[int] = set()
        while container and container not in seen:
            seen.add(container)
            data = self.read_typed(container, ADMINSPACECONTAINER_ID)
            for index in range(self._admin_entries(data)):
                offset = ADMIN_HEADER + index * 8
                space = long_at(data, offset)
                if space and space <= number < space + ADMIN_REGION:
                    edit = self._edit(container, ADMINSPACECONTAINER_ID)
                    put_long(edit, offset + 4, long_at(edit, offset + 4) & ~(1 << (31 - (number - space))))
                    self._pending.pop(number, None)
                    return
            container = long_at(data, HEADER_SIZE)
        raise DataError(f"SFS metadata block {number} is not in any admin space.")

    # ---- the object node tree ---------------------------------------
    @property
    def _nodes_per_leaf(self) -> int:
        return (self.block_size - NODE_CONTAINER_HEADER) // OBJECT_NODE_SIZE

    @property
    def _entries_per_index(self) -> int:
        return (self.block_size - NODE_CONTAINER_HEADER) // 4

    def _node_path(self, node: int) -> tuple[list[tuple[int, int]], int, int]:
        """Return the index path to a node, its leaf block and its offset there."""
        path: list[tuple[int, int]] = []
        block = self.node_root
        for _depth in range(32):
            data = self.read_typed(block, NODECONTAINER_ID)
            first, per_entry = long_at(data, HEADER_SIZE), long_at(data, HEADER_SIZE + 4)
            if per_entry == 1:
                offset = NODE_CONTAINER_HEADER + OBJECT_NODE_SIZE * (node - first)
                if not 0 <= node - first < self._nodes_per_leaf:
                    break
                return path, block, offset
            slot = (node - first) // per_entry
            if not 0 <= slot < self._entries_per_index:
                break
            path.append((block, slot))
            block = long_at(data, NODE_CONTAINER_HEADER + 4 * slot) >> self.node_shift
            if not block:
                break
        raise DataError(f"SFS object node {node} does not exist.")

    def _set_node(self, node: int, *, container: int | None = None,
                  following: int | None = None, hash16: int | None = None) -> None:
        _path, block, offset = self._node_path(node)
        data = self._edit(block, NODECONTAINER_ID)
        if container is not None:
            put_long(data, offset, container)
        if following is not None:
            put_long(data, offset + 4, following)
        if hash16 is not None:
            put_word(data, offset + 8, hash16)

    def _index_full(self, data: bytes) -> bool:
        for slot in range(self._entries_per_index):
            value = long_at(data, NODE_CONTAINER_HEADER + 4 * slot)
            if not value or not value & 1:
                return False
        return True

    def _mark_parents(self, path: list[tuple[int, int]], *, full: bool) -> None:
        """Set or clear the full flag on a leaf's ancestors, as far as it changes."""
        for block, slot in reversed(path):
            data = self._edit(block, NODECONTAINER_ID)
            was_full = self._index_full(data)
            offset = NODE_CONTAINER_HEADER + 4 * slot
            value = long_at(data, offset)
            put_long(data, offset, value | 1 if full else value & ~1)
            if full and not self._index_full(data):
                return
            if not full and not was_full:
                return

    def _create_node(self, container: int, hash16: int) -> int:
        """Allocate an object node pointing at ``container``."""
        for _attempt in range(64):
            path: list[tuple[int, int]] = []
            block = self.node_root
            while True:
                data = self.read_typed(block, NODECONTAINER_ID)
                first, per_entry = long_at(data, HEADER_SIZE), long_at(data, HEADER_SIZE + 4)
                if per_entry == 1:
                    empty = [
                        index for index in range(self._nodes_per_leaf)
                        if not long_at(data, NODE_CONTAINER_HEADER + OBJECT_NODE_SIZE * index)
                    ]
                    if not empty:
                        if block != self.node_root:
                            raise DataError("The SFS node tree marks a full container as having room.")
                        self._add_node_level()
                        break
                    edit = self._edit(block, NODECONTAINER_ID)
                    offset = NODE_CONTAINER_HEADER + OBJECT_NODE_SIZE * empty[0]
                    put_long(edit, offset, container)
                    put_long(edit, offset + 4, 0)
                    put_word(edit, offset + 8, hash16)
                    if len(empty) == 1:
                        self._mark_parents(path, full=True)
                    return first + empty[0]
                slot = next(
                    (
                        index for index in range(self._entries_per_index)
                        if (value := long_at(data, NODE_CONTAINER_HEADER + 4 * index)) and not value & 1
                    ),
                    None,
                )
                if slot is not None:
                    path.append((block, slot))
                    block = long_at(data, NODE_CONTAINER_HEADER + 4 * slot) >> self.node_shift
                    continue
                slot = next(
                    (
                        index for index in range(self._entries_per_index)
                        if not long_at(data, NODE_CONTAINER_HEADER + 4 * index)
                    ),
                    None,
                )
                if slot is None:
                    if block != self.node_root:
                        raise DataError("The SFS node tree marks a full index as having room.")
                    self._add_node_level()
                    break
                child_nodes = 1 if per_entry == self._nodes_per_leaf else per_entry // self._entries_per_index
                child, fresh = self._allocate_admin(NODECONTAINER_ID)
                put_long(fresh, HEADER_SIZE, first + slot * per_entry)
                put_long(fresh, HEADER_SIZE + 4, child_nodes)
                edit = self._edit(block, NODECONTAINER_ID)
                put_long(edit, NODE_CONTAINER_HEADER + 4 * slot, child << self.node_shift)
                path.append((block, slot))
                block = child
        raise DataError("The SFS node tree could not be extended.")

    def _add_node_level(self) -> None:
        """Move the node root's contents down a level, keeping the root block fixed."""
        root = self._edit(self.node_root, NODECONTAINER_ID)
        copy_number, copy = self._allocate_admin(NODECONTAINER_ID)
        copy[:] = root
        per_entry = long_at(root, HEADER_SIZE + 4)
        put_long(
            root,
            HEADER_SIZE + 4,
            self._nodes_per_leaf if per_entry == 1 else per_entry * self._entries_per_index,
        )
        root[NODE_CONTAINER_HEADER:] = bytes(self.block_size - NODE_CONTAINER_HEADER)
        put_long(root, NODE_CONTAINER_HEADER, (copy_number << self.node_shift) | 1)

    def _delete_node(self, node: int) -> None:
        path, block, offset = self._node_path(node)
        data = self._edit(block, NODECONTAINER_ID)
        put_long(data, offset, 0)
        empty = sum(
            1 for index in range(self._nodes_per_leaf)
            if not long_at(data, NODE_CONTAINER_HEADER + OBJECT_NODE_SIZE * index)
        )
        if empty == 1:
            self._mark_parents(path, full=False)
        elif empty == self._nodes_per_leaf:
            self._free_node_container(block, path)

    def _free_node_container(self, block: int, path: list[tuple[int, int]]) -> None:
        if not path:
            return  # The root stays, however empty.
        parent, slot = path[-1]
        self._free_admin(block)
        data = self._edit(parent, NODECONTAINER_ID)
        put_long(data, NODE_CONTAINER_HEADER + 4 * slot, 0)
        if all(
            not long_at(data, NODE_CONTAINER_HEADER + 4 * index)
            for index in range(self._entries_per_index)
        ):
            self._free_node_container(parent, path[:-1])

    # ---- the extent B-tree ------------------------------------------
    def _bt_capacity(self, data: bytes) -> int:
        return (self.block_size - BNODE_CONTAINER_HEADER) // data[HEADER_SIZE + 3]

    @staticmethod
    def _bt_keys(data: bytes) -> list[int]:
        size = data[HEADER_SIZE + 3]
        return [
            long_at(data, BNODE_CONTAINER_HEADER + index * size)
            for index in range(word_at(data, HEADER_SIZE))
        ]

    def _bt_path(self, key: int) -> list[tuple[int, int]]:
        """Return (block, entry index) from the root down to the leaf for ``key``."""
        path: list[tuple[int, int]] = []
        block = self.extent_root
        for _depth in range(32):
            data = self.read_typed(block, BNODECONTAINER_ID)
            keys = self._bt_keys(data)
            index = 0
            for position, value in enumerate(keys):
                if key >= value:
                    index = position
            path.append((block, index))
            if data[HEADER_SIZE + 2] or not keys:
                return path
            block = long_at(data, BNODE_CONTAINER_HEADER + index * BNODE_SIZE + 4)
        raise DataError("The SFS extent tree is too deep to be valid.")

    def _bt_insert_entry(self, block: int, key: int, payload: bytes) -> None:
        data = self._edit(block, BNODECONTAINER_ID)
        size = data[HEADER_SIZE + 3]
        count = word_at(data, HEADER_SIZE)
        keys = self._bt_keys(data)
        position = sum(1 for value in keys if value < key)
        start = BNODE_CONTAINER_HEADER + position * size
        end = BNODE_CONTAINER_HEADER + count * size
        data[start + size : end + size] = data[start:end]
        entry = struct.pack(">I", key) + payload
        data[start : start + size] = entry.ljust(size, b"\0")
        put_word(data, HEADER_SIZE, count + 1)

    def _bt_parent(self, block: int) -> int | None:
        """Find the index container that points at ``block``."""
        if block == self.extent_root:
            return None
        data = self.read_typed(block, BNODECONTAINER_ID)
        key = long_at(data, BNODE_CONTAINER_HEADER)
        current = self.extent_root
        for _depth in range(32):
            parent = self.read_typed(current, BNODECONTAINER_ID)
            if parent[HEADER_SIZE + 2]:
                break
            count = word_at(parent, HEADER_SIZE)
            children = [
                long_at(parent, BNODE_CONTAINER_HEADER + index * BNODE_SIZE + 4)
                for index in range(count)
            ]
            if block in children:
                return current
            index = 0
            for position, value in enumerate(self._bt_keys(parent)):
                if key >= value:
                    index = position
            current = children[index]
        raise DataError(f"SFS extent container {block} has no parent.")

    def _bt_split(self, block: int) -> None:
        """Split a full container, growing the tree at the root when needed."""
        parent = self._bt_parent(block)
        if parent is None:
            # The root block never moves: its contents go down a level and it
            # becomes an index with a single entry for them.
            root = self._edit(block, BNODECONTAINER_ID)
            moved, copy = self._allocate_admin(BNODECONTAINER_ID)
            copy[:] = root
            root[HEADER_SIZE:] = bytes(self.block_size - HEADER_SIZE)
            root[HEADER_SIZE + 2] = 0
            root[HEADER_SIZE + 3] = BNODE_SIZE
            put_word(root, HEADER_SIZE, 1)
            put_long(root, BNODE_CONTAINER_HEADER, 0)
            put_long(root, BNODE_CONTAINER_HEADER + 4, moved)
            parent, block = self.extent_root, moved
        parent_data = self.read_typed(parent, BNODECONTAINER_ID)
        if word_at(parent_data, HEADER_SIZE) >= self._bt_capacity(parent_data):
            self._bt_split(parent)
            parent = self._bt_parent(block)
        data = self._edit(block, BNODECONTAINER_ID)
        size = data[HEADER_SIZE + 3]
        branches = self._bt_capacity(data)
        keep = branches // 2
        count = word_at(data, HEADER_SIZE)
        sibling, fresh = self._allocate_admin(BNODECONTAINER_ID)
        fresh[HEADER_SIZE + 2] = data[HEADER_SIZE + 2]
        fresh[HEADER_SIZE + 3] = size
        moved = data[BNODE_CONTAINER_HEADER + keep * size : BNODE_CONTAINER_HEADER + count * size]
        fresh[BNODE_CONTAINER_HEADER : BNODE_CONTAINER_HEADER + len(moved)] = moved
        put_word(fresh, HEADER_SIZE, count - keep)
        data[BNODE_CONTAINER_HEADER + keep * size :] = bytes(self.block_size - BNODE_CONTAINER_HEADER - keep * size)
        put_word(data, HEADER_SIZE, keep)
        self._bt_insert_entry(parent, long_at(fresh, BNODE_CONTAINER_HEADER), struct.pack(">I", sibling))

    def _extent_insert(self, key: int, following: int, previous: int, blocks: int) -> None:
        payload = struct.pack(">IIH", following, previous, blocks)
        for _attempt in range(64):
            leaf = self._bt_path(key)[-1][0]
            data = self.read_typed(leaf, BNODECONTAINER_ID)
            if word_at(data, HEADER_SIZE) < self._bt_capacity(data):
                self._bt_insert_entry(leaf, key, payload)
                return
            self._bt_split(leaf)
        raise DataError("The SFS extent tree could not be extended.")

    def _bt_remove_key(self, block: int, key: int) -> None:
        data = self._edit(block, BNODECONTAINER_ID)
        size = data[HEADER_SIZE + 3]
        keys = self._bt_keys(data)
        if key not in keys:
            raise DataError(f"The SFS extent tree has no entry for block {key}.")
        position = keys.index(key)
        count = len(keys)
        start = BNODE_CONTAINER_HEADER + position * size
        end = BNODE_CONTAINER_HEADER + count * size
        data[start : end - size] = data[start + size : end]
        data[end - size : end] = bytes(size)
        put_word(data, HEADER_SIZE, count - 1)

    def _bt_delete(self, path: list[tuple[int, int]], key: int) -> None:
        """Remove ``key`` from the last container on ``path`` and rebalance."""
        block = path[-1][0]
        self._bt_remove_key(block, key)
        data = self.read_typed(block, BNODECONTAINER_ID)
        branches = self._bt_capacity(data)
        count = word_at(data, HEADER_SIZE)
        if count >= (branches + 1) // 2:
            return
        if len(path) == 1:
            if count == 1 and not data[HEADER_SIZE + 2]:
                child = long_at(data, BNODE_CONTAINER_HEADER + 4)
                root = self._edit(block, BNODECONTAINER_ID)
                root[:] = self._edit(child, BNODECONTAINER_ID)
                self._free_admin(child)
            return
        parent = path[-2][0]
        parent_data = self.read_typed(parent, BNODECONTAINER_ID)
        parent_count = word_at(parent_data, HEADER_SIZE)
        children = [
            long_at(parent_data, BNODE_CONTAINER_HEADER + index * BNODE_SIZE + 4)
            for index in range(parent_count)
        ]
        position = children.index(block)
        size = data[HEADER_SIZE + 3]
        if position < parent_count - 1:
            neighbour = children[position + 1]
            other = self.read_typed(neighbour, BNODECONTAINER_ID)
            other_count = word_at(other, HEADER_SIZE)
            if other_count + count > branches:
                steal = (other_count + count) // 2 - count
                mine = self._edit(block, BNODECONTAINER_ID)
                theirs = self._edit(neighbour, BNODECONTAINER_ID)
                moved = theirs[BNODE_CONTAINER_HEADER : BNODE_CONTAINER_HEADER + steal * size]
                mine[BNODE_CONTAINER_HEADER + count * size : BNODE_CONTAINER_HEADER + (count + steal) * size] = moved
                put_word(mine, HEADER_SIZE, count + steal)
                rest = theirs[BNODE_CONTAINER_HEADER + steal * size : BNODE_CONTAINER_HEADER + other_count * size]
                theirs[BNODE_CONTAINER_HEADER:] = bytes(self.block_size - BNODE_CONTAINER_HEADER)
                theirs[BNODE_CONTAINER_HEADER : BNODE_CONTAINER_HEADER + len(rest)] = rest
                put_word(theirs, HEADER_SIZE, other_count - steal)
                edit = self._edit(parent, BNODECONTAINER_ID)
                put_long(edit, BNODE_CONTAINER_HEADER + (position + 1) * BNODE_SIZE, long_at(theirs, BNODE_CONTAINER_HEADER))
            else:
                mine = self._edit(block, BNODECONTAINER_ID)
                theirs = self.read_typed(neighbour, BNODECONTAINER_ID)
                moved = theirs[BNODE_CONTAINER_HEADER : BNODE_CONTAINER_HEADER + other_count * size]
                mine[BNODE_CONTAINER_HEADER + count * size : BNODE_CONTAINER_HEADER + count * size + len(moved)] = moved
                put_word(mine, HEADER_SIZE, count + other_count)
                self._free_admin(neighbour)
                parent_key = long_at(parent_data, BNODE_CONTAINER_HEADER + (position + 1) * BNODE_SIZE)
                self._bt_delete(path[:-1], parent_key)
        elif position > 0:
            neighbour = children[position - 1]
            other = self.read_typed(neighbour, BNODECONTAINER_ID)
            other_count = word_at(other, HEADER_SIZE)
            if other_count + count > branches:
                steal = (other_count + count) // 2 - count
                mine = self._edit(block, BNODECONTAINER_ID)
                theirs = self._edit(neighbour, BNODECONTAINER_ID)
                existing = mine[BNODE_CONTAINER_HEADER : BNODE_CONTAINER_HEADER + count * size]
                moved = theirs[BNODE_CONTAINER_HEADER + (other_count - steal) * size : BNODE_CONTAINER_HEADER + other_count * size]
                combined = moved + existing
                mine[BNODE_CONTAINER_HEADER : BNODE_CONTAINER_HEADER + len(combined)] = combined
                put_word(mine, HEADER_SIZE, count + steal)
                theirs[BNODE_CONTAINER_HEADER + (other_count - steal) * size : BNODE_CONTAINER_HEADER + other_count * size] = bytes(steal * size)
                put_word(theirs, HEADER_SIZE, other_count - steal)
                edit = self._edit(parent, BNODECONTAINER_ID)
                put_long(edit, BNODE_CONTAINER_HEADER + position * BNODE_SIZE, long_at(mine, BNODE_CONTAINER_HEADER))
            else:
                theirs = self._edit(neighbour, BNODECONTAINER_ID)
                mine = self.read_typed(block, BNODECONTAINER_ID)
                moved = mine[BNODE_CONTAINER_HEADER : BNODE_CONTAINER_HEADER + count * size]
                theirs[BNODE_CONTAINER_HEADER + other_count * size : BNODE_CONTAINER_HEADER + other_count * size + len(moved)] = moved
                put_word(theirs, HEADER_SIZE, other_count + count)
                self._free_admin(block)
                parent_key = long_at(parent_data, BNODE_CONTAINER_HEADER + position * BNODE_SIZE)
                self._bt_delete(path[:-1], parent_key)

    def _add_extents(self, node: int, runs: list[tuple[int, int]]) -> None:
        for index, (first, count) in enumerate(runs):
            following = runs[index + 1][0] if index + 1 < len(runs) else 0
            previous = (node | 0x80000000) if index == 0 else runs[index - 1][0]
            self._extent_insert(first, following, previous, count)

    def _delete_extents(self, key: int) -> int:
        """Free a file's data and its extent nodes; return the blocks released."""
        released = 0
        seen: set[int] = set()
        while key:
            if key in seen:
                raise DataError("A file's extent chain loops back on itself.")
            seen.add(key)
            following, count = self._extent(key)
            self._mark(key, count, used=False)
            self._bt_delete(self._bt_path(key), key)
            released += count
            key = following
        return released

    # ---- objects -----------------------------------------------------
    def _bump(self, node: int) -> None:
        """Record that a directory changed: new date, archive bit cleared."""
        found = self.object(node)
        data = self._edit(found.container, OBJECTCONTAINER_ID)
        put_long(data, found.offset + 20, datetime_to_sfs(None))
        put_long(data, found.offset + 8, found.protection & ~FIBF_ARCHIVE)

    def _hash_chain(self, table: int, name: str) -> tuple[int, int]:
        data = self.read_typed(table, HASHTABLE_ID)
        chains = (self.block_size - HEADER_SIZE - 4) // 4
        offset = HEADER_SIZE + 4 + 4 * (sfs_hash(name, self.case_sensitive) % chains)
        return offset, long_at(data, offset)

    def _hash_in(self, parent: SFSObject, node: int, name: str) -> None:
        wanted = sfs_hash(name, self.case_sensitive)
        if not parent.first:
            self._set_node(node, hash16=wanted)
            return
        offset, head = self._hash_chain(parent.first, name)
        table = self._edit(parent.first, HASHTABLE_ID)
        put_long(table, offset, node)
        self._set_node(node, following=head, hash16=wanted)

    def _hash_out(self, parent: SFSObject, node: int, name: str) -> None:
        if not parent.first:
            return
        offset, current = self._hash_chain(parent.first, name)
        _container, following, _hash = self.object_node(node)
        if current == node:
            put_long(self._edit(parent.first, HASHTABLE_ID), offset, following)
            return
        seen: set[int] = set()
        while current and current not in seen:
            seen.add(current)
            _container, after, _hash = self.object_node(current)
            if after == node:
                self._set_node(current, following=following)
                return
            current = after
        raise DataError(f"{name} is missing from its directory's hash chain.")

    def _insert_object(
        self,
        parent_node: int,
        *,
        name: str,
        comment: str,
        bits: int,
        protection: int,
        date: int,
        first: int,
        second: int,
        node: int | None = None,
    ) -> SFSObject:
        """Place an object in a directory, reusing ``node`` when it is given."""
        parent = self.object(parent_node)
        needed = object_space(name, comment)
        container = None
        candidate = parent.second
        seen: set[int] = set()
        while candidate and candidate not in seen:
            seen.add(candidate)
            data = self.read_typed(candidate, OBJECTCONTAINER_ID)
            if self.block_size - object_end(data) >= needed:
                container = candidate
                break
            if parent.bits & OTYPE_QUICKDIR:
                break
            candidate = long_at(data, HEADER_SIZE + 4)
        if container is None:
            container, fresh = self._allocate_admin(OBJECTCONTAINER_ID)
            put_long(fresh, HEADER_SIZE, parent_node)
            put_long(fresh, HEADER_SIZE + 4, parent.second)
            put_long(fresh, HEADER_SIZE + 8, 0)
            if parent.second:
                following = self._edit(parent.second, OBJECTCONTAINER_ID)
                put_long(following, HEADER_SIZE + 8, container)
            parent_data = self._edit(parent.container, OBJECTCONTAINER_ID)
            put_long(parent_data, parent.offset + 16, container)
            parent = self.object(parent_node)
        hash16 = sfs_hash(name, self.case_sensitive)
        if node is None:
            node = self._create_node(container, hash16)
        else:
            self._set_node(node, container=container)
        encoded = encode_object(
            node=node, protection=protection, first=first, second=second,
            date=date, bits=bits, name=name, comment=comment,
        )
        data = self._edit(container, OBJECTCONTAINER_ID)
        offset = object_end(data)
        if offset + len(encoded) > self.block_size or offset >= self.block_size - OBJECT_TAIL:
            raise DataError("An SFS object container overflowed while adding an entry.")
        data[offset : offset + len(encoded)] = encoded
        self._hash_in(parent, node, name)
        self._bump(parent_node)
        return self.object(node)

    def _remove_object(self, found: SFSObject, *, keep_node: bool = False) -> None:
        """Take an object out of its directory; free its node unless it moves."""
        data = self.read_typed(found.container, OBJECTCONTAINER_ID)
        parent_node = long_at(data, HEADER_SIZE)
        parent = self.object(parent_node)
        self._hash_out(parent, found.node, found.name)
        objects = self.container_objects(found.container)
        if len(objects) == 1 and found.container != self.root_container:
            self._remove_container(found.container, data, parent)
        else:
            edit = self._edit(found.container, OBJECTCONTAINER_ID)
            length = self._object_length(edit, found.offset)
            tail = edit[found.offset + length :]
            edit[found.offset :] = tail + bytes(length)
        if parent_node == RECYCLED_NODE:
            self._adjust_root_info(
                deleted_files=-1,
                deleted_blocks=-math.ceil(found.size / self.block_size),
            )
        self._bump(parent_node)
        if not keep_node:
            self._delete_node(found.node)

    @staticmethod
    def _object_length(data: bytes, offset: int) -> int:
        name_end = data.index(b"\0", offset + OBJECT_HEADER)
        end = data.index(b"\0", name_end + 1) + 1
        return end - offset + (end & 1)

    def _remove_container(self, number: int, data: bytes, parent: SFSObject) -> None:
        following = long_at(data, HEADER_SIZE + 4)
        previous = long_at(data, HEADER_SIZE + 8)
        if following and following != number:
            put_long(self._edit(following, OBJECTCONTAINER_ID), HEADER_SIZE + 8, previous)
        if previous and previous != number:
            put_long(self._edit(previous, OBJECTCONTAINER_ID), HEADER_SIZE + 4, following)
        else:
            parent_data = self._edit(parent.container, OBJECTCONTAINER_ID)
            put_long(parent_data, parent.offset + 16, following)
        self._free_admin(number)

    def _split_parent(self, path: str) -> tuple[SFSObject, str]:
        parts = split_path(path)
        if not parts:
            raise DataError("The volume root cannot be replaced.")
        parent, _ = self.resolve(join_path(parts[:-1]))
        if not parent.is_dir:
            raise DataError(f"{join_path(parts[:-1])} is not a directory.")
        return parent, validate_sfs_name(parts[-1])

    @staticmethod
    def _stored_protection(meta: AmigaMeta | None) -> int:
        value = DEFAULT_PROTECTION if meta is None else int(meta.protection)
        return (value ^ PERMISSION_BITS) & 0xFFFFFFFF

    @staticmethod
    def _comment(meta: AmigaMeta | None) -> str:
        text = "" if meta is None else str(meta.comment or "")
        try:
            raw = text.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise DataError("The comment uses characters outside the Amiga character set.") from exc
        if len(raw) > MAX_COMMENT_LENGTH:
            raise DataError(f"A comment can hold at most {MAX_COMMENT_LENGTH} characters.")
        return text

    # ---- public changes ----------------------------------------------
    def write_bytes(self, path: str, data: bytes, meta: AmigaMeta | None = None) -> int:
        """Create or replace a file, returning its object node."""
        with self._change():
            parent, name = self._split_parent(path)
            comment = self._comment(meta)
            existing = self.find(parent, name)
            if existing is not None:
                if existing.is_dir:
                    raise DataError(f"{path} is a directory.")
                self._delete_object(existing)
            runs: list[tuple[int, int]] = []
            if data:
                runs = self._allocate_data(math.ceil(len(data) / self.block_size))
                offset = 0
                for first, count in runs:
                    chunk = data[offset : offset + count * self.block_size]
                    padded = chunk.ljust(count * self.block_size, b"\0")
                    self.blocks.write_range(first * self.block_size, padded)
                    offset += len(chunk)
            date = datetime_to_sfs(meta.datestamp if meta is not None else None)
            created = self._insert_object(
                parent.node, name=name, comment=comment, bits=0,
                protection=self._stored_protection(meta), date=date,
                first=0, second=len(data),
            )
            if runs:
                self._add_extents(created.node, runs)
                edit = self._edit(created.container, OBJECTCONTAINER_ID)
                put_long(edit, created.offset + 12, runs[0][0])
            return created.node

    def mkdir(self, path: str) -> int:
        with self._change():
            parent, name = self._split_parent(path)
            if self.find(parent, name) is not None:
                raise DataError(f"{path} already exists.")
            table, fresh = self._allocate_admin(HASHTABLE_ID)
            created = self._insert_object(
                parent.node, name=name, comment="", bits=OTYPE_DIR,
                protection=self._stored_protection(None), date=datetime_to_sfs(None),
                first=table, second=0,
            )
            put_long(self._edit(table, HASHTABLE_ID), HEADER_SIZE, created.node)
            return created.node

    def _delete_object(self, found: SFSObject, *, recursive: bool = False) -> None:
        if found.node == ROOT_NODE:
            raise DataError("The volume root cannot be deleted.")
        if found.bits & OTYPE_UNDELETABLE:
            raise DataError(f"{found.name} is protected by SFS and cannot be deleted.")
        if found.is_dir:
            children = list(self.children(found, include_hidden=True))
            if children and not recursive:
                raise DataError(f"{found.name} is not empty.")
            for child in children:
                self._delete_object(self.object(child.node), recursive=True)
            found = self.object(found.node)
            if found.first:
                self._free_admin(found.first)
        elif found.is_softlink:
            if found.first:
                self._free_admin(found.first)
        elif found.first:
            self._delete_extents(found.first)
        self._remove_object(self.object(found.node))

    def remove(self, path: str, *, recursive: bool = False) -> None:
        with self._change():
            found, _parts = self.resolve(path)
            if not found.protection & 1 and not found.is_dir:
                raise DataError(f"{path} is protected from deletion.")
            self._delete_object(found, recursive=recursive)

    def rename(self, source: str, destination: str) -> None:
        with self._change():
            found, source_parts = self.resolve(source)
            if found.node == ROOT_NODE:
                raise DataError("The volume root cannot be moved.")
            parent, name = self._split_parent(destination)
            existing = self.find(parent, name)
            if existing is not None and existing.node != found.node:
                raise DataError(f"{destination} already exists.")
            if found.is_dir:
                ancestor = parent
                while True:
                    if ancestor.node == found.node:
                        raise DataError("A directory cannot be moved inside itself.")
                    if ancestor.node == ROOT_NODE:
                        break
                    container = self.read_typed(ancestor.container, OBJECTCONTAINER_ID)
                    ancestor = self.object(long_at(container, HEADER_SIZE))
            self._reinsert(found, parent.node, name=name)

    def _reinsert(self, found: SFSObject, parent_node: int, *, name: str | None = None,
                  comment: str | None = None, protection: int | None = None,
                  date: int | None = None) -> SFSObject:
        """Rewrite an object, possibly elsewhere, keeping its node and contents."""
        self._remove_object(found, keep_node=True)
        return self._insert_object(
            parent_node,
            name=found.name if name is None else name,
            comment=found.comment if comment is None else comment,
            bits=found.bits,
            protection=found.protection if protection is None else protection,
            date=found.date if date is None else date,
            first=found.first,
            second=found.second,
            node=found.node,
        )

    def _update_in_place(self, found: SFSObject, *, protection: int | None = None,
                         date: int | None = None) -> None:
        data = self._edit(found.container, OBJECTCONTAINER_ID)
        if protection is not None:
            put_long(data, found.offset + 8, protection)
        if date is not None:
            put_long(data, found.offset + 20, date)

    def set_amiga_meta(self, path: str, meta: AmigaMeta) -> None:
        with self._change():
            found, _parts = self.resolve(path)
            comment = self._comment(meta)
            protection = (int(meta.protection) ^ PERMISSION_BITS) & 0xFFFFFFFF
            date = datetime_to_sfs(meta.datestamp) if meta.datestamp is not None else None
            if comment == found.comment or found.node == ROOT_NODE:
                self._update_in_place(found, protection=protection, date=date)
                return
            parent = long_at(self.read_typed(found.container, OBJECTCONTAINER_ID), HEADER_SIZE)
            self._reinsert(found, parent, comment=comment, protection=protection,
                           date=found.date if date is None else date)

    def set_access(self, path: str, access) -> None:
        value = access.value if hasattr(access, "value") else int(access)
        meta = self.amiga_meta(path)
        self.set_amiga_meta(path, meta.with_protection(value))

    def set_comment(self, path: str, value: str) -> None:
        self.set_amiga_meta(path, self.amiga_meta(path).with_comment(value))

    def set_datestamp(self, path: str, moment) -> None:
        with self._change():
            found, _parts = self.resolve(path)
            self._update_in_place(found, date=datetime_to_sfs(moment))

    def set_title(self, value: str) -> None:
        """Rename the volume, which is the name of the root object."""
        with self._change():
            name = validate_sfs_name(value)
            if len(name) > 30:
                raise DataError("An SFS volume name can be at most 30 characters long.")
            root = self.root_object()
            encoded = encode_object(
                node=root.node, protection=root.protection, first=root.first,
                second=root.second, date=root.date, bits=root.bits,
                name=name, comment=root.comment,
            )
            data = self._edit(self.root_container, OBJECTCONTAINER_ID)
            limit = self.block_size - ROOT_INFO_SIZE
            if CONTAINER_HEADER + len(encoded) > limit:
                raise DataError("That volume name does not fit in the root block.")
            data[CONTAINER_HEADER:limit] = encoded + bytes(limit - CONTAINER_HEADER - len(encoded))

    def set_boot_option(self, option: int) -> None:
        raise DataError(
            "SFS has no boot block options. Whether a partition boots is set in "
            "the drive's partition table."
        )

    def defragment(self) -> int:
        raise DataError(
            "SFS volumes are defragmented by SFS itself, with SFSDefrag on the Amiga."
        )


def format_sfs_volume(
    reader: BlockReader,
    *,
    label: str = "Empty",
    block_size: int = 512,
    reserved: int = 2,
    prealloc: int = 1,
    recycled: bool = True,
    case_sensitive: bool = False,
) -> None:
    """Write a new, empty SFS volume across the whole of ``reader``.

    The layout is the one SFS's own format command writes: the admin space
    container at the first unreserved block, then the root object container,
    its hash table, the transaction marker, the extent and node tree roots and
    the hidden ``.recycled`` directory, all inside the first 32-block admin
    region; the bitmap straight after that region; and a root block at each
    end. ``reserved`` and ``prealloc`` are the partition's reserved blocks at
    each end, as the RDB records them.
    """
    name = validate_sfs_name(label)
    if len(name) > 30:
        raise DataError("An SFS volume name can be at most 30 characters long.")
    # The volume is written through a handle of its own, at its own block
    # size. Anything the caller has written and not yet flushed would reach
    # the file after it, so it is flushed first.
    reader.flush()
    if block_size < 512 or block_size & (block_size - 1) or block_size % reader.block_size:
        raise DataError("The SFS block size must be a power of two of at least 512 bytes.")
    blocks = reader.reopen(writable=True, block_size=block_size)
    try:
        total = blocks.total_blocks
        per_page = (block_size - HEADER_SIZE) * 8
        bitmap_blocks = math.ceil(total / per_page)
        start = max(reserved, 1)
        end = max(prealloc, 1)
        admin = start
        root = start + 1
        extent_root = root + 3
        node_root = root + 4
        recycled_block = root + 5
        bitmap_base = admin + ADMIN_REGION
        if bitmap_base + bitmap_blocks + end + ADMIN_REGION >= total:
            raise DataError("The partition is too small for an SFS volume.")
        now = datetime_to_sfs(None)
        chains = (block_size - HEADER_SIZE - 4) // 4
        recycled_hash = sfs_hash(".recycled", case_sensitive)

        def write(number: int, block: bytearray) -> None:
            blocks.write_block(number, seal_sfs_block(block, number))

        def fresh(block_id: bytes) -> bytearray:
            block = bytearray(block_size)
            block[:4] = block_id
            return block

        container = fresh(ADMINSPACECONTAINER_ID)
        container[HEADER_SIZE + 8] = ADMIN_REGION
        put_long(container, ADMIN_HEADER, admin)
        put_long(container, ADMIN_HEADER + 4, 0xFE000000 if recycled else 0xFC000000)
        write(admin, container)

        root_container = fresh(OBJECTCONTAINER_ID)
        root_object = encode_object(
            node=ROOT_NODE, protection=PERMISSION_BITS, first=root + 1,
            second=recycled_block if recycled else 0, date=now, bits=OTYPE_DIR, name=name,
        )
        root_container[CONTAINER_HEADER : CONTAINER_HEADER + len(root_object)] = root_object
        info = block_size - ROOT_INFO_SIZE
        put_long(root_container, info + 8, total - ADMIN_REGION - start - end - bitmap_blocks)
        put_long(root_container, info + 12, now)
        write(root, root_container)

        table = fresh(HASHTABLE_ID)
        put_long(table, HEADER_SIZE, ROOT_NODE)
        if recycled:
            put_long(table, HEADER_SIZE + 4 + 4 * (recycled_hash % chains), RECYCLED_NODE)
        write(root + 1, table)

        write(root + 2, fresh(TRANSACTIONOK_ID))

        extents = fresh(BNODECONTAINER_ID)
        extents[HEADER_SIZE + 2] = 1
        extents[HEADER_SIZE + 3] = EXTENT_NODE_SIZE
        write(extent_root, extents)

        nodes = fresh(NODECONTAINER_ID)
        put_long(nodes, HEADER_SIZE, 1)
        put_long(nodes, HEADER_SIZE + 4, 1)
        put_long(nodes, NODE_CONTAINER_HEADER, root)
        second = NODE_CONTAINER_HEADER + OBJECT_NODE_SIZE
        if recycled:
            put_long(nodes, second, recycled_block)
            put_word(nodes, second + 8, recycled_hash)
        else:
            put_long(nodes, second, 0xFFFFFFFF)
        for index in range(2, 6):
            put_long(nodes, NODE_CONTAINER_HEADER + OBJECT_NODE_SIZE * index, 0xFFFFFFFF)
        write(node_root, nodes)

        if recycled:
            bin_container = fresh(OBJECTCONTAINER_ID)
            put_long(bin_container, HEADER_SIZE, ROOT_NODE)
            bin_object = encode_object(
                node=RECYCLED_NODE, protection=0x0C, first=0, second=0, date=now,
                bits=OTYPE_DIR | OTYPE_UNDELETABLE | OTYPE_QUICKDIR | OTYPE_HIDDEN,
                name=".recycled",
            )
            bin_container[CONTAINER_HEADER : CONTAINER_HEADER + len(bin_object)] = bin_object
            write(recycled_block, bin_container)

        # A bit is set for a free block, the first block of a page in the
        # highest bit. The free blocks are one run, from the end of the
        # bitmap to the reserved blocks at the end of the partition, so each
        # page is a run of ones cut from that, worked out as one number
        # rather than a bit at a time: a partition of a hundred gigabytes has
        # two hundred million of them.
        used_start = ADMIN_REGION + bitmap_blocks + start
        free_end = total - end
        for page in range(bitmap_blocks):
            first = page * per_page
            low = max(used_start, first) - first
            high = min(free_end, first + per_page) - first
            value = 0
            if high > low:
                value = ((1 << (high - low)) - 1) << (per_page - high)
            bitmap = fresh(BITMAP_ID)
            bitmap[HEADER_SIZE:] = value.to_bytes(per_page // 8, "big")
            write(bitmap_base + page, bitmap)

        root_block = fresh(SFS_ID)
        put_word(root_block, 12, STRUCTURE_VERSION)
        put_long(root_block, 16, now)
        root_block[20] = (ROOTBITS_CASESENSITIVE if case_sensitive else 0) | (
            ROOTBITS_RECYCLED if recycled else 0
        )
        low = reader.offset
        high = reader.offset + total * block_size
        put_long(root_block, 0x20, low >> 32)
        put_long(root_block, 0x24, low)
        put_long(root_block, 0x28, high >> 32)
        put_long(root_block, 0x2C, high)
        put_long(root_block, 0x30, total)
        put_long(root_block, 0x34, block_size)
        put_long(root_block, 0x60, bitmap_base)
        put_long(root_block, 0x64, admin)
        put_long(root_block, 0x68, root)
        put_long(root_block, 0x6C, extent_root)
        put_long(root_block, 0x70, node_root)
        write(0, bytearray(root_block))
        write(total - 1, bytearray(root_block))
        blocks.sync()
    finally:
        blocks.close()


__all__ = ["SFSWriter", "format_sfs_volume"]
