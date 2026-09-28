"""Rebuilding an OFS or FFS block-allocation bitmap from the catalogue.

This is the repair the Amiga's own disk validator performs: every block the
directory tree can reach is marked in use and everything else is marked free.
It is only sound when the whole tree can be walked without contradiction, so
any damaged chain, out-of-range block or block claimed twice abandons the
rebuild before anything is written.

The vendored engine has no such operation, so this module uses its internals.
They are confined here and covered by the engine contract tests.
"""

from __future__ import annotations

from typing import Any

from amigafs._vendor.amiganut.errors import DataError
from amigafs._vendor.amiganut.filesystem.amigados import _tail
from amigafs._vendor.amiganut.filesystem.blocks import (
    ST_FILE,
    ST_USERDIR,
    apply_checksum,
    long_at,
    put_long,
    signed_long_at,
)

BITMAP_VALID = 0xFFFFFFFF
ROOT_BITMAP_POINTERS = 25


def _bitmap_pages(volume: Any) -> tuple[list[int], list[int]]:
    """Return the bitmap blocks and bitmap extension blocks the root names."""

    root = volume.reader.read_block(volume.root_block)
    pages: list[int] = []
    base = _tail(volume.block_size, 196)
    for index in range(ROOT_BITMAP_POINTERS):
        block = long_at(root, base + index * 4)
        if block:
            pages.append(block)
    extensions: list[int] = []
    extension = long_at(root, _tail(volume.block_size, 96))
    seen = set(pages)
    while extension:
        if extension in seen or not volume.reserved <= extension < volume.total_blocks:
            raise DataError("The bitmap extension chain is damaged.")
        seen.add(extension)
        extensions.append(extension)
        page = volume.reader.read_block(extension)
        for index in range(volume.block_size // 4 - 1):
            block = long_at(page, index * 4)
            if block:
                if block in seen:
                    raise DataError("A bitmap block is listed twice.")
                seen.add(block)
                pages.append(block)
        extension = long_at(page, volume.block_size - 4)
    return pages, extensions


def reachable_blocks(volume: Any) -> set[int]:
    """Return every block the root block and its directory tree own."""

    pages, extensions = _bitmap_pages(volume)
    covered = volume.total_blocks - volume.reserved
    per_page = (volume.block_size // 4 - 1) * 32
    if len(pages) * per_page < covered:
        raise DataError("The root block does not name enough bitmap blocks for this volume.")
    used: set[int] = set()

    def claim(block: int, owner: str) -> None:
        if not volume.reserved <= block < volume.total_blocks:
            raise DataError(f"{owner} names block {block}, which is outside the volume.")
        if block in used:
            raise DataError(f"Block {block} is claimed twice; {owner} is one of its owners.")
        used.add(block)

    claim(volume.root_block, "the root block")
    for block in pages:
        claim(block, "the bitmap")
    for block in extensions:
        claim(block, "the bitmap extension chain")

    pending = [volume.root_block]
    while pending:
        directory = pending.pop()
        if volume.dircache:
            for entry in volume._load_cache(directory):
                claim(int(entry[0]), "a directory cache")
        for child in volume._chain_blocks(directory):
            header = volume._read_header(child)
            name = volume._header_name(header, child)
            claim(child, name)
            comment_block = volume._comment_block_of(header, child)
            if comment_block:
                claim(comment_block, f"the comment of {name}")
            secondary = signed_long_at(header, _tail(volume.block_size, 4))
            if secondary == ST_USERDIR:
                pending.append(child)
            elif secondary == ST_FILE:
                for block in volume._data_blocks(child):
                    claim(block, name)
                extension = long_at(header, _tail(volume.block_size, 8))
                while extension:
                    claim(extension, f"the extension chain of {name}")
                    extension = long_at(volume._read_header(extension), _tail(volume.block_size, 8))
    return used


def rebuild_bitmap(mount: Any) -> int:
    """Rewrite the bitmap from the catalogue and mark it valid.

    Returns the number of blocks marked in use.
    """

    volume = mount.volume
    if not hasattr(volume, "_chain_blocks") or not hasattr(volume, "_store_bitmap"):
        raise DataError("Only OFS and FFS volumes have a bitmap AmigaFS can rebuild.")
    volume._require_writable()
    pages, _extensions = _bitmap_pages(volume)
    used = reachable_blocks(volume)
    covered = volume.total_blocks - volume.reserved
    bits = bytearray(b"\x01" * covered)
    for block in used:
        bits[block - volume.reserved] = 0
    volume._bitmap = bits
    volume._bitmap_blocks = pages
    volume._dirty_bitmap = True
    volume._dirty_pages = set()
    root = bytearray(volume.reader.read_block(volume.root_block))
    put_long(root, _tail(volume.block_size, 200), BITMAP_VALID)
    volume.reader.write_block(volume.root_block, bytes(apply_checksum(root)))
    volume._store_bitmap()
    return len(used)


__all__ = ["reachable_blocks", "rebuild_bitmap"]
