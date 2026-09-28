"""Ranged reads of large files, without loading the whole file.

The vendored engine reads a file whole. A file too large for the bounded cache
is instead read a range at a time from its block list. That needs a few engine
internals, which are confined to this module and covered by the engine contract
tests. Any file this module does not fully understand is declined, and the
caller falls back to the engine's own whole-file read.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, field
from itertools import accumulate
from typing import Any

from amigafs._vendor.amiganut.filesystem import PFS3Mount, SFSMount
from amigafs._vendor.amiganut.filesystem.blocks import ST_FILE

MAX_TRACKED_FILES = 16


@dataclass(frozen=True, slots=True)
class _Layout:
    """Where one file's bytes live: runs of whole blocks in file order."""

    block_size: int
    runs: tuple[tuple[int, int], ...]
    size: int
    sfs: bool = False
    starts: tuple[int, ...] = field(init=False)
    capacity: int = field(init=False)

    def __post_init__(self) -> None:
        ends = tuple(accumulate(count * self.block_size for _first, count in self.runs))
        object.__setattr__(self, "starts", (0, *ends[:-1]) if ends else ())
        object.__setattr__(self, "capacity", ends[-1] if ends else 0)


class RangedReader:
    """Block layouts for the most recently read large files."""

    def __init__(self) -> None:
        self._layouts: OrderedDict[int, _Layout | None] = OrderedDict()

    def clear(self) -> None:
        self._layouts.clear()

    def _layout(self, mount: Any, path: str) -> _Layout | None:
        volume = mount.volume
        stat = mount.stat(path)
        if stat.is_dir or stat.secondary_type != ST_FILE:
            return None
        if isinstance(mount, SFSMount):
            found, _parts = volume.resolve(path)
            runs = tuple(volume.extents(found.first)) if stat.length else ()
            return _Layout(int(volume.block_size), runs, int(stat.length), sfs=True)
        if isinstance(mount, PFS3Mount):
            found, _parts = volume.resolve(path)
            if getattr(found.extra, "virtualsize", 0):
                return None
            runs = tuple(volume.extents(found.anode)) if stat.length else ()
            return _Layout(int(volume.block_size), runs, int(stat.length))
        if not getattr(volume, "ffs", False):
            # OFS data blocks each carry a header and their own length.
            return None
        blocks = volume._data_blocks(stat.block)
        runs_list: list[tuple[int, int]] = []
        for block in blocks:
            if runs_list and runs_list[-1][0] + runs_list[-1][1] == block:
                runs_list[-1] = (runs_list[-1][0], runs_list[-1][1] + 1)
            else:
                runs_list.append((block, 1))
        return _Layout(int(volume.block_size), tuple(runs_list), int(stat.length))

    def read(self, mount: Any, inode: int, path: str, offset: int, length: int) -> bytes | None:
        """Return the requested range, or ``None`` when the file must be read whole."""

        if inode in self._layouts:
            layout = self._layouts[inode]
            self._layouts.move_to_end(inode)
        else:
            try:
                layout = self._layout(mount, path)
            except Exception:
                layout = None
            self._layouts[inode] = layout
            while len(self._layouts) > MAX_TRACKED_FILES:
                self._layouts.popitem(last=False)
        if layout is None:
            return None
        if layout.capacity < layout.size:
            return None
        end = min(layout.size, offset + length)
        if offset >= end:
            return b""
        volume = mount.volume
        parts: list[bytes] = []
        index = max(0, bisect_right(layout.starts, offset) - 1)
        while index < len(layout.runs):
            first, count = layout.runs[index]
            position = layout.starts[index]
            if position >= end:
                break
            run_end = position + count * layout.block_size
            skip_blocks = max(0, offset - position) // layout.block_size
            last_block = -(-(min(end, run_end) - position) // layout.block_size)
            wanted = last_block - skip_blocks
            if layout.sfs:
                data = volume.read_run(first + skip_blocks, wanted)
            elif hasattr(volume, "blocks"):
                data = volume.blocks.read_range(
                    (first + skip_blocks) * layout.block_size, wanted * layout.block_size
                )
            else:
                data = volume.reader.read_range(
                    (first + skip_blocks) * layout.block_size, wanted * layout.block_size
                )
            start = position + skip_blocks * layout.block_size
            low = max(offset, start) - start
            high = min(end, start + len(data)) - start
            parts.append(bytes(data[low:high]))
            index += 1
        result = b"".join(parts)
        return result if len(result) == end - offset else None


__all__ = ["RangedReader"]
