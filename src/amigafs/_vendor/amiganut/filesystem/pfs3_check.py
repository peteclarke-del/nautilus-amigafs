"""Checking a PFS3 volume the way PFSDoctor and DiskValid do.

The check makes one pass over the reserved trees and the directory tree,
recording what each structure claims, and then compares the claims with the
three allocation maps the volume keeps: the reserved bitmap in the root
cluster, the anode blocks, and the main bitmap. Memory stays bounded on a
large partition: reserved blocks and anodes are tracked one bit each, file
data as a list of extents rather than a bit per block, and the main bitmap is
read one block at a time.
"""

from __future__ import annotations

from ..errors import DataError
from .blocks import ST_FILE, ST_LINKDIR, ST_LINKFILE, ST_SOFTLINK, ST_USERDIR
from .pfs3_blocks import (
    ANODE_ROOTDIR,
    ANODE_SIZE,
    ANODEBLOCK_HEADER,
    ANODEBLOCK_ID,
    BITMAPBLOCK_ID,
    BITMAPINDEX_ID,
    BLOCK_HEADER,
    DELDIR_ENTRY,
    DELDIR_ID,
    DELDIRBLOCK_HEADER,
    DELENTRIES_PER_BLOCK,
    DIRBLOCK_ID,
    EMPTY_BLOCKNR,
    EXT_DELDIR,
    EXT_DELDIRSIZE,
    EXT_SUPERINDEX,
    INDEXBLOCK_ID,
    KNOWN_MODES,
    MAXBITMAPINDEX,
    MAXSMALLBITMAPINDEX,
    MAXSMALLINDEXNR,
    MAXSUPER,
    MODE_DELDIR,
    MODE_SUPERDELDIR,
    RESERVED_BITMAP,
    ROOT_BITMAPINDEX,
    ROOT_BLOCKSFREE,
    ROOT_DELDIR,
    ROOT_RESERVED_FREE,
    ROOT_SMALL_INDEX,
    ST_ROLLOVERFILE,
    SUPERBLOCK_ID,
    count_set_bits,
    fold,
    iter_block_entries,
    u16,
    u32,
)

KNOWN_TYPES = (ST_USERDIR, ST_SOFTLINK, ST_LINKDIR, ST_FILE, ST_LINKFILE, ST_ROLLOVERFILE)


class _Checker:
    def __init__(self, volume):
        self.volume = volume
        self.problems: list[str] = []
        self.reserved = bytearray((volume.num_reserved + 7) // 8)
        self.anode_blocks: dict[int, int] = {}
        self.anodes = bytearray()
        self.runs: list[tuple[int, int, str]] = []

    def problem(self, text: str) -> None:
        if text not in self.problems:
            self.problems.append(text)

    # ---- marking ----------------------------------------------------------
    def claim_reserved(self, number: int, what: str) -> bool:
        volume = self.volume
        offset = number - volume.firstreserved
        index, misaligned = divmod(offset, volume.rescluster)
        if number < volume.firstreserved or number > volume.lastreserved or misaligned:
            self.problem(f"The {what} at block {number} lies outside the reserved area.")
            return False
        mask = 0x80 >> (index % 8)
        if self.reserved[index // 8] & mask:
            self.problem(f"Reserved block {number} is used twice; the second use is the {what}.")
            return False
        self.reserved[index // 8] |= mask
        return True

    def anode_index(self, number: int) -> int:
        seqnr, slot = self.volume._split(number)
        if seqnr not in self.anode_blocks or slot >= self.volume.anodes_per_block:
            return -1
        return seqnr * self.volume.anodes_per_block + slot

    def claim_anode(self, number: int, owner: str) -> bool:
        index = self.anode_index(number)
        if index < 0:
            self.problem(f"{owner} uses anode {number:#x}, which lies in no anode block.")
            return False
        mask = 0x80 >> (index % 8)
        if self.anodes[index // 8] & mask:
            self.problem(f"Anode {number:#x} is used twice; the second use is {owner}.")
            return False
        self.anodes[index // 8] |= mask
        return True

    # ---- the reserved trees -----------------------------------------------
    def check_root(self) -> None:
        volume = self.volume
        if volume.options & ~KNOWN_MODES:
            self.problem(f"The root block enables options this build does not know ({volume.options:#x}).")
        pending = volume.pending_operation()
        if pending:
            self.problem(
                f"PFS3 recorded a postponed operation it has not finished ({pending}); "
                "it completes it when the volume is next mounted on an Amiga."
            )
        cluster = u16(volume._root, 66)
        for number in range(volume.firstreserved, volume.firstreserved + cluster, volume.rescluster):
            self.claim_reserved(number, "root cluster")
        if volume.extension_block:
            self.claim_reserved(volume.extension_block, "root block extension")

    def check_anode_tree(self) -> None:
        volume = self.volume
        per = volume.index_per_block
        index_blocks: list[tuple[int, int]] = []
        if volume.supermode:
            ext = volume._ext()
            for super_nr in range(MAXSUPER + 1):
                number = u32(ext, EXT_SUPERINDEX + 4 * super_nr)
                if not number:
                    continue
                data = self.typed(number, SUPERBLOCK_ID, "super block", super_nr)
                if data is None:
                    continue
                for slot in range(per):
                    child = u32(data, BLOCK_HEADER + 4 * slot)
                    if child:
                        index_blocks.append((super_nr * per + slot, child))
        else:
            for index_nr in range(MAXSMALLINDEXNR + 1):
                number = u32(volume._root, ROOT_SMALL_INDEX + 4 * index_nr)
                if number:
                    index_blocks.append((index_nr, number))
        for index_nr, number in index_blocks:
            data = self.typed(number, INDEXBLOCK_ID, "anode index block", index_nr)
            if data is None:
                continue
            for slot in range(per):
                child = u32(data, BLOCK_HEADER + 4 * slot)
                if child:
                    seqnr = index_nr * per + slot
                    if self.typed(child, ANODEBLOCK_ID, "anode block", seqnr) is not None:
                        self.anode_blocks[seqnr] = child
        if not self.anode_blocks:
            raise DataError("The PFS3 anode index lists no anode blocks at all.")
        highest = max(self.anode_blocks)
        self.anodes = bytearray(((highest + 1) * volume.anodes_per_block + 7) // 8)
        # Anodes 0 to 4 are reserved and always in use.
        for number in range(ANODE_ROOTDIR):
            self.claim_anode(number, "the reserved anodes")

    def check_bitmap_tree(self) -> None:
        volume = self.volume
        per = volume.index_per_block
        limit = MAXBITMAPINDEX if volume.supermode else MAXSMALLBITMAPINDEX
        found: set[int] = set()
        for index_nr in range(limit + 1):
            number = u32(volume._root, ROOT_BITMAPINDEX + 4 * index_nr)
            if not number:
                continue
            data = self.typed(number, BITMAPINDEX_ID, "bitmap index block", index_nr)
            if data is None:
                continue
            for slot in range(per):
                child = u32(data, BLOCK_HEADER + 4 * slot)
                if child:
                    seqnr = index_nr * per + slot
                    if self.typed(child, BITMAPBLOCK_ID, "bitmap block", seqnr) is not None:
                        found.add(seqnr)
        missing = [seqnr for seqnr in range(volume.bitmap_blocks_needed) if seqnr not in found]
        if missing:
            raise DataError(
                f"The PFS3 bitmap is missing {len(missing)} of its {volume.bitmap_blocks_needed} "
                f"blocks, starting with block {missing[0]}."
            )

    def typed(self, number: int, block_id: bytes, what: str, seqnr: int | None = None):
        if not self.claim_reserved(number, what):
            return None
        try:
            data = self.volume._typed(number, block_id, what)
        except DataError as error:
            self.problem(str(error))
            return None
        if seqnr is not None and u32(data, 8) != seqnr:
            self.problem(f"The {what} at block {number} carries sequence number {u32(data, 8)}, not {seqnr}.")
        return data

    def check_deldir(self) -> None:
        volume = self.volume
        if not volume.options & MODE_DELDIR:
            return
        blocks: list[int] = []
        if volume.options & MODE_SUPERDELDIR:
            ext = volume._ext()
            if ext is not None:
                count = u16(ext, EXT_DELDIRSIZE)
                blocks = [u32(ext, EXT_DELDIR + 4 * index) for index in range(min(count, 32))]
        elif u32(volume._root, ROOT_DELDIR):
            blocks = [u32(volume._root, ROOT_DELDIR)]
        for seqnr, number in enumerate(blocks):
            if not number:
                self.problem(f"Deleted-files directory block {seqnr} is missing.")
                continue
            data = self.typed(number, DELDIR_ID, "deleted-files directory block")
            if data is None:
                continue
            for slot in range(DELENTRIES_PER_BLOCK):
                offset = DELDIRBLOCK_HEADER + slot * DELDIR_ENTRY
                if offset + DELDIR_ENTRY > len(data):
                    break
                anode = u32(data, offset)
                if anode:
                    # A deleted file keeps its anodes, though its blocks are free.
                    try:
                        for node in volume.anode_chain(anode):
                            self.claim_anode(node.number, "a file in the deleted-files directory")
                    except DataError:
                        pass

    # ---- the directory tree -------------------------------------------------
    def check_tree(self) -> None:
        volume = self.volume
        stack: list[tuple[int, int, str]] = [(ANODE_ROOTDIR, 0, "")]
        while stack:
            main, parent, path = stack.pop()
            where = path or ":"
            try:
                chain = volume.anode_chain(main)
            except DataError as error:
                self.problem(f"{where}: {error}")
                continue
            names: set[bytes] = set()
            for node in chain:
                self.claim_anode(node.number, f"directory {where}")
                if node.clustersize != 1:
                    self.problem(f"A directory anode of {where} spans {node.clustersize} blocks, not 1.")
                data = self.typed(node.blocknr, DIRBLOCK_ID, f"directory block of {where}")
                if data is None:
                    continue
                if u32(data, 12) != main:
                    self.problem(f"Directory block {node.blocknr} of {where} names the wrong directory.")
                if u32(data, 16) != parent:
                    self.problem(f"Directory block {node.blocknr} of {where} names the wrong parent.")
                try:
                    entries = list(iter_block_entries(
                        data, dir_extension=volume.dir_extension, largefile=volume.largefile,
                        block=node.blocknr, directory=main,
                    ))
                except DataError as error:
                    self.problem(f"{where}: {error}")
                    continue
                for entry in entries:
                    child = f"{path}/{entry.name}" if path else entry.name
                    folded = fold(entry.raw_name)
                    if folded in names:
                        self.problem(f"{child} appears twice in its directory.")
                    names.add(folded)
                    self.check_entry(entry, child, main, stack)

    def check_entry(self, entry, path: str, directory: int, stack) -> None:
        volume = self.volume
        if entry.type not in KNOWN_TYPES:
            self.problem(f"{path} has the unknown entry type {entry.type}.")
            return
        if not entry.raw_name or len(entry.raw_name) > volume.fnsize:
            self.problem(f"{path} has a name longer than this volume allows.")
        if entry.type in (ST_LINKFILE, ST_LINKDIR):
            self.check_link(entry, path, directory)
            return
        if entry.extra.link:
            self.check_link_chain(entry, path, directory)
        if entry.type == ST_USERDIR:
            stack.append((entry.anode, directory, path))
            return
        try:
            chain = volume.anode_chain(entry.anode)
        except DataError as error:
            self.problem(f"{path}: {error}")
            return
        blocks = 0
        for node in chain:
            self.claim_anode(node.number, path)
            if node.clustersize and node.blocknr not in (0, EMPTY_BLOCKNR):
                if node.blocknr < volume.bitmap_start or node.blocknr + node.clustersize > volume.total_blocks:
                    self.problem(f"{path} has data outside the data area of the volume.")
                    continue
                self.runs.append((node.blocknr, node.clustersize, path))
                blocks += node.clustersize
            elif node.clustersize:
                self.problem(f"{path} has an anode that counts blocks but names none.")
        size = entry.fsize if entry.type == ST_ROLLOVERFILE else entry.size
        if blocks != -(-size // volume.block_size):
            self.problem(f"{path} has {blocks:,} blocks for {size:,} bytes.")
        if entry.type == ST_ROLLOVERFILE and (
            entry.extra.virtualsize > entry.fsize or entry.extra.rollpointer > entry.fsize
        ):
            self.problem(f"The rollover file {path} has ring pointers beyond its size.")

    def check_link(self, entry, path: str, directory: int) -> None:
        volume = self.volume
        if not entry.extra.link:
            self.problem(f"The hard link {path} names no object.")
            return
        self.claim_anode(entry.anode, f"the hard link {path}")
        try:
            node = volume.anode(entry.anode)
            if node.blocknr != directory:
                self.problem(f"The hard link {path} records the wrong directory for itself.")
            if volume.find_by_anode(node.clustersize, entry.extra.link) is None:
                self.problem(f"The hard link {path} points at an object that is not there.")
        except DataError as error:
            self.problem(f"{path}: {error}")

    def check_link_chain(self, entry, path: str, directory: int) -> None:
        seen: set[int] = set()
        number = entry.extra.link
        try:
            while number:
                if number in seen:
                    self.problem(f"The hard link chain of {path} loops.")
                    return
                seen.add(number)
                node = self.volume.anode(number)
                if node.clustersize != directory:
                    self.problem(f"A hard link to {path} records the wrong directory for it.")
                number = node.next
        except DataError as error:
            self.problem(f"{path}: {error}")

    # ---- the allocation maps --------------------------------------------------
    def compare_anodes(self) -> None:
        volume = self.volume
        per = volume.anodes_per_block
        unused = lost = 0
        first_unused = first_lost = None
        for seqnr, number in sorted(self.anode_blocks.items()):
            data = volume._reserved(number)
            for slot in range(per):
                index = seqnr * per + slot
                offset = ANODEBLOCK_HEADER + slot * ANODE_SIZE
                allocated = any(data[offset : offset + ANODE_SIZE])
                used = bool(self.anodes[index // 8] & (0x80 >> (index % 8)))
                if allocated and not used:
                    unused += 1
                    first_unused = first_unused if first_unused is not None else volume._join(seqnr, slot)
                elif used and not allocated:
                    lost += 1
                    first_lost = first_lost if first_lost is not None else volume._join(seqnr, slot)
        if lost:
            self.problem(f"{lost} anodes in use are marked free, starting with {first_lost:#x}.")
        if unused:
            self.problem(f"{unused} anodes are allocated but nothing refers to them, starting with {first_unused:#x}.")

    def compare_reserved(self) -> None:
        volume = self.volume
        count = volume.num_reserved
        length = (count + 7) // 8
        pad = length * 8 - count
        # Both maps as one integer each, most significant bit first, so the
        # comparison is a handful of whole-map operations however large.
        marked_free = int.from_bytes(volume._root[RESERVED_BITMAP : RESERVED_BITMAP + length], "big") >> pad
        used = int.from_bytes(self.reserved, "big") >> pad
        everything = (1 << count) - 1
        clash = marked_free & used
        if clash:
            first = volume.firstreserved + (count - clash.bit_length()) * volume.rescluster
            self.problem(f"{clash.bit_count()} reserved blocks in use are marked free, starting with block {first}.")
        unused = ~marked_free & ~used & everything
        if unused:
            self.problem(f"{unused.bit_count()} reserved blocks are marked in use but nothing refers to them.")
        free = marked_free.bit_count()
        recorded = u32(volume._root, ROOT_RESERVED_FREE)
        if recorded != free:
            self.problem(f"The volume records {recorded:,} free reserved blocks but its reserved bitmap has {free:,}.")

    def compare_bitmap(self) -> None:
        volume = self.volume
        runs = sorted(self.runs)
        for (first, count, owner), (following, _count, other) in zip(runs, runs[1:]):
            if following < first + count:
                self.problem(f"Blocks from {following} belong to both {owner} and {other}.")
                break
        per = volume.bits_per_bitmap
        data_bits = volume.total_blocks - volume.bitmap_start
        free = 0
        referenced = sum(count for _first, count, _owner in runs)
        position = 0
        reported_free = False
        for seqnr in range(volume.bitmap_blocks_needed):
            payload = volume._bitmap_payload(seqnr)
            low = seqnr * per
            covered = min(per, data_bits - low)
            free += count_set_bits(payload, 0, covered)
            high = low + covered
            while position < len(runs):
                first, count, owner = runs[position]
                start = first - volume.bitmap_start
                if start >= high:
                    break
                stop = min(start + count, high)
                if not reported_free and count_set_bits(payload, max(start, low) - low, stop - low):
                    self.problem(f"{owner} holds blocks the bitmap marks free.")
                    reported_free = True
                if start + count > high:
                    # The run continues into the next bitmap block.
                    runs[position] = (volume.bitmap_start + high, start + count - high, owner)
                    break
                position += 1
        used = data_bits - free
        if used > referenced:
            self.problem(f"{used - referenced:,} data blocks are marked in use but belong to no file.")
        recorded = u32(volume._root, ROOT_BLOCKSFREE)
        if recorded != free:
            self.problem(f"The volume records {recorded:,} free blocks but its bitmap has {free:,}.")

    def run(self) -> list[str]:
        self.check_root()
        self.check_anode_tree()
        self.check_bitmap_tree()
        self.check_deldir()
        self.check_tree()
        self.compare_anodes()
        self.compare_reserved()
        self.compare_bitmap()
        return self.problems


def check_volume(volume) -> list[str]:
    checker = _Checker(volume)
    try:
        return checker.run()
    except DataError as error:
        return checker.problems + [str(error)]


__all__ = ["check_volume"]
