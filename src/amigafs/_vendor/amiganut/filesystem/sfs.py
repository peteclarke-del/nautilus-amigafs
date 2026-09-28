"""The Smart File System (SFS), as written by SFS 1.x on AmigaOS.

SFS shares nothing with the AmigaDOS Fast File System below the directory
level, so it has its own volume class rather than a branch in ``amigados``.
The structures are these, all in big-endian longs:

* Two root blocks, one at the start of the partition and one at the end, with
  the id ``SFS\\0``. They record the block size, which is often larger than a
  sector, and where every other structure starts.
* Object containers (``OBJC``). Each holds as many directory entries as fit,
  all belonging to one directory, and the containers of a directory are
  chained. An entry carries a node number rather than a block number, so it
  can move between containers without its references changing.
* A node tree (``NDC ``) mapping every node number to the container that
  currently holds that object, plus the next node in its hash chain.
* One hash table block (``HTAB``) per directory, so a name can be found
  without walking the whole directory.
* An extent B-tree (``BNDC``). A file names the first block of its data, and
  that block is the key of an extent node giving the run length and the key
  of the next run.
* A free-space bitmap (``BTMP``), 1 meaning free, most significant bit first.

Every block but a data block starts with the same header: the id, a checksum
chosen so that one plus the sum of every long is zero, and the block's own
number as a check against misdirected reads.

SFS makes changes crash-safe with a transaction log. Before it overwrites any
metadata it writes the new contents of every block it will change to free
space, then puts a ``TRFA`` block two blocks after the root object container
pointing at that log. Only then are the real blocks written, and the marker
goes back to ``TROK``. A drive that lost power part way through therefore
carries a log that SFS replays when it next mounts the volume. This module
replays such a log in memory when it reads, so an interrupted volume is
presented as SFS would present it rather than as a half-written one.

The structure definitions were taken from the SFS sources published under
the LGPL, as carried in the AROS tree under ``rom/filesys/SFS``. No code is
shared with them; only the on-disk format is.
"""

from __future__ import annotations

import struct
from datetime import datetime

from ..errors import DataError
from ..file import AmigaMeta
from .amigados import Entry, Stat, join_path, split_path
from .blocks import BlockReader
from .sfs_blocks import (
    ADMIN_HEADER,
    ADMIN_REGION,
    ADMINSPACECONTAINER_ID,
    BITMAP_ID,
    BNODE_CONTAINER_HEADER,
    BNODE_SIZE,
    BNODECONTAINER_ID,
    CONTAINER_HEADER,
    HASHTABLE_ID,
    HEADER_SIZE,
    MAX_NAME_LENGTH,
    NODE_CONTAINER_HEADER,
    NODECONTAINER_ID,
    OBJECT_NODE_SIZE,
    OBJECTCONTAINER_ID,
    OI_DELETE,
    OI_EMPTY,
    ROOT_INFO_SIZE,
    ROOT_NODE,
    ROOTBITS_CASESENSITIVE,
    SFS_ID,
    SOFTLINK_ID,
    STRUCTURE_VERSION,
    TRANSACTIONFAILURE_ID,
    TRANSACTIONSTORAGE_ID,
    SFSObject,
    long_at,
    parse_objects,
    sfs_date,
    sfs_hash,
    uncompress_operation,
    upper_char,
    verify_sfs_block,
    word_at,
)
from .sfs_write import SFSWriter


class SFSVolume(SFSWriter):
    """One SFS partition, read through a window onto its image or drive."""

    format = "SFS"
    name_limit = MAX_NAME_LENGTH

    def __init__(self, reader: BlockReader):
        self.sector_reader = reader
        self.writable = reader.writable
        header = reader.read_block(0)
        block_size = long_at(header, 0x34) if header[:4] == SFS_ID else 0
        self.root = self._select_root(reader, block_size)
        self.block_size = long_at(self.root, 0x34)
        self.total_blocks = long_at(self.root, 0x30)
        self.blocks = reader.reopen(block_size=self.block_size)
        if self.blocks.total_blocks < self.total_blocks:
            self.blocks.close()
            raise DataError(
                f"The SFS volume declares {self.total_blocks:,} blocks but its "
                f"partition holds only {self.blocks.total_blocks:,}."
            )
        self.case_sensitive = bool(self.root[20] & ROOTBITS_CASESENSITIVE)
        self.bitmap_base = long_at(self.root, 0x60)
        self.admin_space = long_at(self.root, 0x64)
        self.root_container = long_at(self.root, 0x68)
        self.extent_root = long_at(self.root, 0x6C)
        self.node_root = long_at(self.root, 0x70)
        self.node_shift = self.block_size.bit_length() - 1 - 5
        self.overlay: dict[int, bytes] = {}
        self.recovered_transaction = False
        self._cache: dict[int, bytes] = {}
        self._verified: set[int] = set()
        self._parsed: dict[int, list[SFSObject]] = {}
        self._reset_changes()
        self._load_pending_transaction()

    # ---- root selection ---------------------------------------------
    @staticmethod
    def _select_root(reader: BlockReader, declared_size: int) -> bytes:
        """Return the valid root block with the highest sequence number.

        SFS keeps a copy at each end of the partition and accepts either,
        so a volume whose first root was overwritten still opens.
        """
        sizes = [declared_size] if declared_size else []
        sizes += [size for size in (512, 1024, 2048, 4096, 8192, 16384, 32768) if size not in sizes]
        partition_bytes = reader.length
        candidates: list[bytes] = []
        for size in sizes:
            if size % reader.block_size or partition_bytes < size * 2:
                continue
            last = partition_bytes // size - 1
            for number in (0, last):
                data = reader.read_range(number * size, size)
                if (
                    verify_sfs_block(data, number, SFS_ID)
                    and word_at(data, 12) == STRUCTURE_VERSION
                    and long_at(data, 0x34) == size
                ):
                    candidates.append(data)
            if candidates:
                break
        if not candidates:
            raise DataError(
                "No valid SFS root block was found at either end of the partition."
            )
        return max(candidates, key=lambda data: word_at(data, 14))

    # ---- block access -----------------------------------------------
    #: Metadata blocks kept in memory. A drive attached over USB answers each
    #: read slowly, and a directory walk revisits the same node and object
    #: containers many times.
    CACHE_BLOCKS = 16384

    def read(self, number: int) -> bytes:
        if number in self._pending:
            return bytes(self._pending[number])
        if number in self.overlay:
            return self.overlay[number]
        cached = self._cache.get(number)
        if cached is not None:
            return cached
        data = self.blocks.read_block(number)
        if len(self._cache) >= self.CACHE_BLOCKS:
            self._forget(next(iter(self._cache)))
        self._cache[number] = data
        return data

    def _forget(self, number: int) -> None:
        """Drop everything remembered about one block."""
        self._cache.pop(number, None)
        self._verified.discard(number)
        self._parsed.pop(number, None)

    def _remember(self, number: int, data: bytes) -> None:
        """Record a sealed block that has just been written."""
        self._forget(number)
        self._cache[number] = data
        self._verified.add(number)

    def read_typed(self, number: int, block_id: bytes) -> bytes:
        data = self.read(number)
        if number in self._pending:
            # A block changed in the current transaction is sealed only when
            # it is written, so only its identity can be checked here.
            if data[:4] != block_id:
                raise DataError(
                    f"SFS block {number} should be {block_id.decode('latin-1').strip()}."
                )
            return data
        if number in self._verified and data[:4] == block_id:
            return data
        if not verify_sfs_block(data, number, block_id):
            found = data[:4].decode("latin-1", "replace")
            raise DataError(
                f"SFS block {number} should be {block_id.decode('latin-1').strip()} "
                f"but holds {found!r} or fails its checksum."
            )
        if number in self._cache:
            self._verified.add(number)
        return data

    def container_objects(self, number: int) -> list[SFSObject]:
        """The objects in one container, parsed once while the block is unchanged."""
        if number in self._pending:
            return parse_objects(self.read_typed(number, OBJECTCONTAINER_ID), number)
        parsed = self._parsed.get(number)
        if parsed is None:
            parsed = parse_objects(self.read_typed(number, OBJECTCONTAINER_ID), number)
            if number in self._cache or number in self.overlay:
                self._parsed[number] = parsed
        return parsed

    def read_run(self, number: int, count: int) -> bytes:
        """Read ``count`` consecutive blocks, for file data."""
        if not 0 <= number or number + count > self.total_blocks:
            raise DataError(f"Blocks {number} to {number + count - 1} are outside this volume.")
        data = self.blocks.read_range(number * self.block_size, count * self.block_size)
        if self.overlay:
            patched = bytearray(data)
            for block in range(number, number + count):
                if block in self.overlay:
                    start = (block - number) * self.block_size
                    patched[start : start + self.block_size] = self.overlay[block]
            data = bytes(patched)
        return data

    # ---- transactions -----------------------------------------------
    def _load_pending_transaction(self) -> None:
        """Replay, in memory, a transaction SFS did not finish writing."""
        marker_block = self.root_container + 2
        marker = self.blocks.read_block(marker_block)
        if not verify_sfs_block(marker, marker_block, TRANSACTIONFAILURE_ID):
            return
        stream = bytearray()
        block = long_at(marker, HEADER_SIZE)
        seen: set[int] = set()
        while block:
            if block in seen:
                raise DataError("The SFS transaction log loops back on itself.")
            seen.add(block)
            storage = self.blocks.read_block(block)
            if not verify_sfs_block(storage, block, TRANSACTIONSTORAGE_ID):
                raise DataError(
                    "This SFS volume holds an unfinished transaction whose log is "
                    "damaged. Mount it on an Amiga so SFS can recover it."
                )
            stream += storage[HEADER_SIZE + 4 :]
            block = long_at(storage, HEADER_SIZE)
        for number, bits, payload in self._operations(bytes(stream)):
            if bits & OI_DELETE:
                self.overlay.pop(number, None)
                continue
            base = (
                bytes(self.block_size)
                if bits & OI_EMPTY
                else self.overlay.get(number) or self.blocks.read_block(number)
            )
            self.overlay[number] = uncompress_operation(base, payload)
        self.recovered_transaction = True

    @staticmethod
    def _operations(stream: bytes):
        """Split a transaction log into (block, bits, compressed data) entries.

        Each entry is a 16-bit length, the block number, one byte of bits and
        the data, padded so the next entry starts on an even byte. A length of
        zero ends the log.
        """
        offset = 0
        while offset + 7 <= len(stream):
            length = word_at(stream, offset)
            if length == 0:
                return
            number = long_at(stream, offset + 2)
            bits = stream[offset + 6]
            payload = stream[offset + 7 : offset + 7 + length]
            if len(payload) != length:
                raise DataError("The SFS transaction log is truncated.")
            yield number, bits, payload
            offset += (length | 1) + 7

    # ---- nodes and objects ------------------------------------------
    def object_node(self, node: int) -> tuple[int, int, int]:
        """Return the container block, next hash node and hash of one object."""
        block = self.node_root
        for _depth in range(32):
            data = self.read_typed(block, NODECONTAINER_ID)
            first = long_at(data, HEADER_SIZE)
            per_entry = long_at(data, HEADER_SIZE + 4)
            if node < first:
                break
            if per_entry == 1:
                offset = HEADER_SIZE + 8 + OBJECT_NODE_SIZE * (node - first)
                if offset + OBJECT_NODE_SIZE > self.block_size:
                    break
                return (
                    long_at(data, offset),
                    long_at(data, offset + 4),
                    word_at(data, offset + 8),
                )
            slot = (node - first) // per_entry
            offset = HEADER_SIZE + 8 + 4 * slot
            if offset + 4 > self.block_size:
                break
            block = long_at(data, offset) >> self.node_shift
            if not block:
                break
        raise DataError(f"SFS object node {node} does not exist.")

    def object(self, node: int) -> SFSObject:
        container, _next, _hash = self.object_node(node)
        for candidate in self.container_objects(container):
            if candidate.node == node:
                return candidate
        raise DataError(f"SFS object {node} is missing from container {container}.")

    def root_object(self) -> SFSObject:
        return self.container_objects(self.root_container)[0]

    def children(self, directory: SFSObject, *, include_hidden: bool = False):
        """Yield every object in a directory, following its container chain."""
        container = directory.second
        seen: set[int] = set()
        while container:
            if container in seen:
                raise DataError(f"The directory {directory.name} has a looping container chain.")
            seen.add(container)
            data = self.read_typed(container, OBJECTCONTAINER_ID)
            for candidate in self.container_objects(container):
                if include_hidden or not candidate.hidden:
                    yield candidate
            container = long_at(data, HEADER_SIZE + 4)

    def _names_match(self, left: str, right: str) -> bool:
        if self.case_sensitive:
            return left == right
        try:
            fold = lambda text: bytes(upper_char(code) for code in text.encode("latin-1"))
            return fold(left) == fold(right)
        except UnicodeEncodeError:
            return False

    def find(self, directory: SFSObject, name: str) -> SFSObject | None:
        """Find one name in a directory, through its hash table when it has one."""
        if directory.first:
            try:
                table = self.read_typed(directory.first, HASHTABLE_ID)
                wanted = sfs_hash(name, self.case_sensitive)
                chains = (self.block_size - HEADER_SIZE - 4) // 4
                node = long_at(table, HEADER_SIZE + 4 + 4 * (wanted % chains))
                seen: set[int] = set()
                while node and node not in seen:
                    seen.add(node)
                    container, following, stored = self.object_node(node)
                    if stored == wanted:
                        candidate = self.object(node)
                        if self._names_match(candidate.name, name):
                            return candidate
                    node = following
                return None
            except (DataError, UnicodeEncodeError):
                pass
        for candidate in self.children(directory, include_hidden=True):
            if self._names_match(candidate.name, name):
                return candidate
        return None

    def resolve(self, path: str | None) -> tuple[SFSObject, list[str]]:
        parts = split_path(path)
        current = self.root_object()
        for index, part in enumerate(parts):
            if not current.is_dir:
                raise DataError(f"{join_path(parts[:index])} is not a directory.")
            found = self.find(current, part)
            if found is None:
                raise DataError(f"Path not found: {join_path(parts[: index + 1])}")
            current = found
        return current, parts

    # ---- volume identity --------------------------------------------
    @property
    def title(self) -> str:
        return self.root_object().name

    def root_info(self) -> dict:
        data = self.read_typed(self.root_container, OBJECTCONTAINER_ID)
        base = self.block_size - ROOT_INFO_SIZE
        values = struct.unpack_from(">9I", data, base)
        keys = (
            "deletedBlocks", "deletedFiles", "freeBlocks", "dateCreated",
            "lastAllocatedBlock", "lastAllocatedAdminSpace",
            "lastAllocatedExtentNode", "lastAllocatedObjectNode", "rovingPointer",
        )
        return dict(zip(keys, values))

    def size_bytes(self) -> int:
        return self.total_blocks * self.block_size

    def free_bytes(self) -> int:
        return self.root_info()["freeBlocks"] * self.block_size

    def used_bytes(self) -> int:
        return self.size_bytes() - self.free_bytes()

    # ---- traversal ---------------------------------------------------
    def exists(self, path: str | None) -> bool:
        try:
            self.resolve(path)
        except DataError:
            return False
        return True

    def stat(self, path: str | None) -> Stat:
        found, parts = self.resolve(path)
        return Stat(
            name=parts[-1] if parts else found.name,
            path=join_path(parts),
            is_dir=found.is_dir,
            length=found.size,
            blocks=-(-found.size // self.block_size) if found.size else 1,
            block=found.container,
            secondary_type=found.secondary_type,
        )

    def iter_entries(self, path: str | None = None):
        directory, parts = self.resolve(path)
        if not directory.is_dir:
            raise DataError(f"{join_path(parts)} is not a directory.")
        prefix = join_path(parts)
        for child in self.children(directory):
            yield Entry(
                name=child.name,
                path=f"{prefix}/{child.name}" if prefix else child.name,
                is_dir=child.is_dir,
                length=child.size,
                block=child.container,
                secondary_type=child.secondary_type,
            )

    # ---- reading -----------------------------------------------------
    def extents(self, key: int) -> list[tuple[int, int]]:
        """Return a file's data as (first block, block count) runs, in order."""
        runs: list[tuple[int, int]] = []
        seen: set[int] = set()
        while key:
            if key in seen:
                raise DataError("A file's extent chain loops back on itself.")
            seen.add(key)
            following, count = self._extent(key)
            runs.append((key, count))
            key = following
        return runs

    def _extent(self, key: int) -> tuple[int, int]:
        block = self.extent_root
        for _depth in range(32):
            data = self.read_typed(block, BNODECONTAINER_ID)
            count = word_at(data, HEADER_SIZE)
            leaf = data[HEADER_SIZE + 2]
            size = data[HEADER_SIZE + 3]
            base = HEADER_SIZE + 4
            chosen = None
            for index in range(count):
                offset = base + index * size
                if long_at(data, offset) <= key:
                    chosen = offset
                else:
                    break
            if chosen is None:
                break
            if leaf:
                if long_at(data, chosen) != key:
                    break
                return long_at(data, chosen + 4), word_at(data, chosen + 12)
            block = long_at(data, chosen + 4)
        raise DataError(f"No extent starts at block {key}; the file's data cannot be found.")

    def read_bytes(self, path: str) -> bytes:
        found, parts = self.resolve(path)
        if found.is_dir:
            raise DataError(f"{join_path(parts)} is not a file.")
        if found.is_softlink:
            return self.link_target(found).encode("latin-1")
        return self.read_object(found)

    def read_object(self, found: SFSObject) -> bytes:
        size = found.second
        chunks: list[bytes] = []
        remaining = size
        for first, count in self.extents(found.first) if size else ():
            if remaining <= 0:
                break
            chunk = self.read_run(first, count)
            chunks.append(chunk[:remaining])
            remaining -= len(chunks[-1])
        data = b"".join(chunks)
        if len(data) < size:
            raise DataError(
                f"{found.name} declares {size:,} bytes but its extents hold only "
                f"{len(data):,}. The file is truncated."
            )
        return data

    def link_target(self, found: SFSObject) -> str:
        data = self.read_typed(found.first, SOFTLINK_ID)
        text = data[CONTAINER_HEADER:]
        return text[: text.index(b"\0") if b"\0" in text else len(text)].decode("latin-1")

    # ---- metadata ----------------------------------------------------
    def amiga_meta(self, path: str) -> AmigaMeta:
        found, _parts = self.resolve(path)
        return AmigaMeta(
            protection=found.dos_protection,
            comment=found.comment,
            datestamp=sfs_date(found.date),
        )

    def access(self, path: str):
        return self.amiga_meta(path).access

    def comment(self, path: str) -> str:
        return self.amiga_meta(path).comment

    def datestamp(self, path: str) -> datetime:
        return self.amiga_meta(path).datestamp

    # ---- free space --------------------------------------------------
    def free_map(self) -> list[bool]:
        """Return one flag per block: True when the bitmap marks it free."""
        per_block = (self.block_size - HEADER_SIZE) * 8
        pages = -(-self.total_blocks // per_block)
        bits: list[bool] = []
        for page in range(pages):
            data = self.read_typed(self.bitmap_base + page, BITMAP_ID)
            value = int.from_bytes(data[HEADER_SIZE:], "big")
            text = format(value, f"0{per_block}b")
            bits.extend(character == "1" for character in text)
        return bits[: self.total_blocks]

    def free_block_count(self) -> int:
        """Count free blocks from the bitmap itself rather than the cached total."""
        per_block = (self.block_size - HEADER_SIZE) * 8
        pages = -(-self.total_blocks // per_block)
        free = 0
        for page in range(pages):
            data = self.read_typed(self.bitmap_base + page, BITMAP_ID)
            covered = min(per_block, self.total_blocks - page * per_block)
            value = int.from_bytes(data[HEADER_SIZE:], "big") >> (per_block - covered)
            free += value.bit_count()
        return free

    # ---- changes -----------------------------------------------------
    def boot_option(self) -> int:
        """SFS has no boot block options; a partition boots through the RDB."""
        return 0

    # ---- maintenance -------------------------------------------------
    def validate(self) -> list[str]:
        """Check every structure the way SFScheck does, and report what is wrong.

        The walk covers the admin space, the extent B-tree, the object node
        tree, every directory's containers and hash chain, every file's
        extents, and the bitmap, then checks that they agree: each metadata
        block is recorded in the admin space, each object's node points at its
        container, each extent chain is linked both ways, no block belongs to
        two files, and nothing in use is marked free.
        """
        problems: list[str] = []
        if self.recovered_transaction:
            problems.append(
                "SFS did not finish writing its last change. It is shown as SFS "
                "would recover it; the next change made here completes it."
            )
        try:
            free = self._free_text()
        except DataError as error:
            return problems + [str(error)]
        metadata: set[int] = set()

        def typed(number: int, block_id: bytes) -> bytes:
            metadata.add(number)
            return self.read_typed(number, block_id)

        try:
            admin_used = self._check_admin_space(typed, free, problems)
            extents = self._check_extent_tree(typed, problems)
            nodes = self._check_node_tree(typed, problems)
            runs = self._check_catalogue(typed, nodes, extents, problems)
        except DataError as error:
            return problems + [str(error)]
        runs.sort()
        for (first, count, owner), (following, _count, other) in zip(runs, runs[1:]):
            if following < first + count:
                problems.append(f"Blocks from {following} belong to both {owner} and {other}.")
                break
        for first, count, owner in runs:
            if "1" in free[first : first + count]:
                problems.append(f"{owner} holds blocks the bitmap marks free.")
                break
        bitmap_end = self.bitmap_base + -(-self.total_blocks // ((self.block_size - HEADER_SIZE) * 8))
        for number in sorted(metadata):
            if number in (0, self.total_blocks - 1) or self.bitmap_base <= number < bitmap_end:
                continue
            if number not in admin_used:
                problems.append(f"Metadata block {number} is not recorded in the admin space.")
                break
        cached = self.root_info()["freeBlocks"]
        counted = free.count("1")
        if counted != cached:
            problems.append(
                f"The volume records {cached:,} free blocks but its bitmap has {counted:,}."
            )
        return problems

    def _free_text(self) -> str:
        """The whole bitmap as a string of '1' (free) and '0' (used) per block."""
        per_page = (self.block_size - HEADER_SIZE) * 8
        pages = -(-self.total_blocks // per_page)
        parts = []
        for page in range(pages):
            data = self.read_typed(self.bitmap_base + page, BITMAP_ID)
            parts.append(format(int.from_bytes(data[HEADER_SIZE:], "big"), f"0{per_page}b"))
        return "".join(parts)[: self.total_blocks]

    def _check_admin_space(self, typed, free: str, problems: list[str]) -> set[int]:
        used: set[int] = set()
        container = self.admin_space
        seen: set[int] = set()
        while container:
            if container in seen:
                problems.append("The admin space container chain loops.")
                break
            seen.add(container)
            data = typed(container, ADMINSPACECONTAINER_ID)
            for index in range((self.block_size - ADMIN_HEADER) // 8):
                offset = ADMIN_HEADER + index * 8
                space, bits = long_at(data, offset), long_at(data, offset + 4)
                if not space:
                    continue
                if "1" in free[space : space + ADMIN_REGION]:
                    problems.append(f"The admin region at block {space} is marked free.")
                for bit in range(ADMIN_REGION):
                    if bits & (1 << (31 - bit)):
                        used.add(space + bit)
            container = long_at(data, HEADER_SIZE)
        return used

    def _check_extent_tree(self, typed, problems: list[str]) -> dict[int, tuple[int, int, int]]:
        extents: dict[int, tuple[int, int, int]] = {}
        depths: set[int] = set()

        def walk(number: int, low: int, high: int, depth: int, root: bool) -> None:
            data = typed(number, BNODECONTAINER_ID)
            count = word_at(data, HEADER_SIZE)
            leaf, size = data[HEADER_SIZE + 2], data[HEADER_SIZE + 3]
            if not root and not count:
                problems.append(f"Extent container {number} is empty.")
            keys = [long_at(data, BNODE_CONTAINER_HEADER + index * size) for index in range(count)]
            if keys != sorted(set(keys)):
                problems.append(f"Extent container {number} is out of order.")
            for index, key in enumerate(keys):
                if not low <= key < high and not (index == 0 and not leaf and key == 0):
                    problems.append(f"Extent key {key} is outside the range of container {number}.")
            if leaf:
                depths.add(depth)
                for index, key in enumerate(keys):
                    offset = BNODE_CONTAINER_HEADER + index * size
                    extents[key] = (
                        long_at(data, offset + 4), long_at(data, offset + 8), word_at(data, offset + 12)
                    )
                return
            for index, key in enumerate(keys):
                child = long_at(data, BNODE_CONTAINER_HEADER + index * BNODE_SIZE + 4)
                walk(child, key if index else low, keys[index + 1] if index + 1 < count else high, depth + 1, False)

        walk(self.extent_root, 0, 1 << 32, 0, True)
        if len(depths) > 1:
            problems.append("The extent tree's leaves are at different depths.")
        return extents

    def _check_node_tree(self, typed, problems: list[str]) -> dict[int, int]:
        per_leaf = (self.block_size - NODE_CONTAINER_HEADER) // OBJECT_NODE_SIZE
        per_index = (self.block_size - NODE_CONTAINER_HEADER) // 4
        nodes: dict[int, int] = {}

        def walk(number: int, first: int, per_entry: int | None) -> bool:
            data = typed(number, NODECONTAINER_ID)
            if long_at(data, HEADER_SIZE) != first:
                problems.append(f"Node container {number} starts at the wrong node number.")
            stored = long_at(data, HEADER_SIZE + 4)
            if per_entry is not None and stored != per_entry:
                problems.append(f"Node container {number} covers the wrong number of nodes.")
            if stored == 1:
                full = True
                for index in range(per_leaf):
                    value = long_at(data, NODE_CONTAINER_HEADER + OBJECT_NODE_SIZE * index)
                    if value == 0:
                        full = False
                    elif value != 0xFFFFFFFF:
                        nodes[first + index] = value
                return full
            child_nodes = 1 if stored == per_leaf else stored // per_index
            full = True
            for index in range(per_index):
                value = long_at(data, NODE_CONTAINER_HEADER + 4 * index)
                if not value:
                    full = False
                    continue
                child_full = walk(value >> self.node_shift, first + index * stored, child_nodes)
                if bool(value & 1) != child_full:
                    problems.append(f"Node container {number} has a wrong full flag.")
                full = full and child_full
            return full

        walk(self.node_root, 1, None)
        return nodes

    def _check_catalogue(self, typed, nodes, extents, problems: list[str]) -> list[tuple[int, int, str]]:
        runs: list[tuple[int, int, str]] = []
        seen_nodes = {ROOT_NODE}
        used_extents: set[int] = set()

        def walk(directory: SFSObject, prefix: str) -> None:
            container, previous = directory.second, 0
            chain: set[int] = set()
            members: dict[int, str] = {}
            while container:
                if container in chain:
                    problems.append(f"The directory {prefix or ':'} has a looping container chain.")
                    return
                chain.add(container)
                data = typed(container, OBJECTCONTAINER_ID)
                if long_at(data, HEADER_SIZE) != directory.node:
                    problems.append(f"Container {container} names the wrong parent directory.")
                if long_at(data, HEADER_SIZE + 8) != previous:
                    problems.append(f"Container {container} links back to the wrong container.")
                children = self.container_objects(container)
                if not children:
                    problems.append(f"The empty container {container} is still linked into {prefix or ':'}.")
                for child in children:
                    path = f"{prefix}/{child.name}" if prefix else child.name
                    seen_nodes.add(child.node)
                    members[child.node] = child.name
                    if nodes.get(child.node) != container:
                        problems.append(f"{path} is not where its object node says it is.")
                    if child.is_dir:
                        if child.first:
                            table = typed(child.first, HASHTABLE_ID)
                            if long_at(table, HEADER_SIZE) != child.node:
                                problems.append(f"The hash table of {path} names the wrong directory.")
                        walk(child, path)
                    elif child.is_softlink:
                        typed(child.first, SOFTLINK_ID)
                    elif child.second:
                        self._check_file(child, path, extents, used_extents, runs, problems)
                previous = container
                container = long_at(data, HEADER_SIZE + 4)
            if directory.first:
                self._check_hash_table(directory, prefix, members, problems)

        root = self.root_object()
        if root.first:
            typed(root.first, HASHTABLE_ID)
        walk(root, "")
        for node in nodes:
            if node not in seen_nodes:
                problems.append(f"Object node {node} is in use but no entry refers to it.")
                break
        for key in extents:
            if key not in used_extents:
                problems.append(f"The extent at block {key} belongs to no file.")
                break
        return runs

    def _check_hash_table(self, directory: SFSObject, prefix: str, members: dict[int, str],
                          problems: list[str]) -> None:
        """Check that every entry of a directory sits once in the right hash chain."""
        table = self.read_typed(directory.first, HASHTABLE_ID)
        chains = (self.block_size - HEADER_SIZE - 4) // 4
        hashed: set[int] = set()
        where = prefix or ":"
        for chain in range(chains):
            node = long_at(table, HEADER_SIZE + 4 + 4 * chain)
            while node:
                if node in hashed:
                    problems.append(f"A hash chain in {where} loops or repeats an entry.")
                    return
                hashed.add(node)
                name = members.get(node)
                try:
                    _container, following, stored = self.object_node(node)
                except DataError as error:
                    problems.append(f"{where}: {error}")
                    return
                if name is None:
                    problems.append(f"A hash chain in {where} holds node {node}, which is not in it.")
                elif stored != sfs_hash(name, self.case_sensitive) or stored % chains != chain:
                    problems.append(f"{prefix}/{name} is hashed under the wrong value." if prefix else f"{name} is hashed under the wrong value.")
                node = following
        missing = set(members) - hashed
        if missing:
            name = members[min(missing)]
            problems.append(f"{prefix + '/' if prefix else ''}{name} is missing from its directory's hash table.")

    def _check_file(self, found, path, extents, used_extents, runs, problems) -> None:
        key, back, total = found.first, found.node | 0x80000000, 0
        seen: set[int] = set()
        while key:
            if key in seen or key not in extents:
                problems.append(f"{path} has a broken extent chain.")
                return
            seen.add(key)
            used_extents.add(key)
            following, previous, count = extents[key]
            if previous != back:
                problems.append(f"{path} has an extent that links back to the wrong place.")
            if key + count > self.total_blocks:
                problems.append(f"{path} has data beyond the end of the volume.")
            runs.append((key, count, path))
            total += count
            back, key = key, following
        if total != -(-found.second // self.block_size):
            problems.append(f"{path} has {total:,} blocks for {found.second:,} bytes.")

    def flush(self) -> None:
        self.blocks.flush()

    def close(self) -> None:
        try:
            self.blocks.close()
        finally:
            self.sector_reader.close()


__all__ = ["SFSVolume"]
