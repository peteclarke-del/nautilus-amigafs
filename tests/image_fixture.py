"""Generated Amiga test media; no private images are required."""

from __future__ import annotations

import gzip
import struct
from collections.abc import Iterable
from pathlib import Path

from amigafs._vendor.amiganut.file import AmigaMeta
from amigafs._vendor.amiganut.filesystem.blocks import apply_checksum
from amigafs.core.create import CreatedImage, create_floppy_image, create_hard_disc_image
from amigafs.core.image import ROOT_INODE, AmigaImage

BLOCK = 512
DD_ROOT_BLOCK = 880

#: The files every populated fixture holds, as ``(path, data, protection, comment)``.
STANDARD_FILES: tuple[tuple[str, bytes, int, str], ...] = (
    ("S/Startup-Sequence", b"; boot\nLoadWB\nEndCLI\n", 0x40, "Boot script"),
    ("C/List", bytes(range(256)) * 12, 0, ""),
    ("Docs/ReadMe", b"Read me first.\n" * 40, 0, "Introduction"),
    ("Docs/Deep/Nested/Note", b"nested", 0, ""),
    ("Empty", b"", 0, ""),
)


def populate(image: AmigaImage, root: int, files: Iterable[tuple[str, bytes, int, str]]) -> None:
    """Create drawers and files below one directory inode."""

    for path, data, protection, comment in files:
        parent = root
        *drawers, leaf = path.split("/")
        for drawer in drawers:
            found = image.lookup(parent, drawer.encode())
            if found is None:
                found = image.make_directory(parent, drawer.encode())
            parent = found.inode
        image.import_file(
            parent, leaf.encode(), data, AmigaMeta(protection=protection, comment=comment)
        )


def create_floppy(
    directory: Path,
    *,
    name: str = "floppy",
    filesystem: str = "FFS",
    density: str = "dd",
    title: str = "Workbench",
    bootable: bool = False,
    files: Iterable[tuple[str, bytes, int, str]] = STANDARD_FILES,
) -> Path:
    """Create a populated, validated ADF."""

    created = create_floppy_image(
        directory,
        name=name,
        title=title,
        density=density,
        filesystem=filesystem,
        bootable=bootable,
    )
    with AmigaImage.open(created.path, writable=True) as image:
        populate(image, ROOT_INODE, files)
    return created.path


def create_empty_floppy(directory: Path, *, name: str = "empty", filesystem: str = "OFS") -> Path:
    return create_floppy_image(directory, name=name, filesystem=filesystem).path


def create_hard_disc(
    directory: Path,
    *,
    name: str = "harddisk",
    filesystem: str = "FFS-INTL",
    capacity: str = "8MB",
    partitions: int = 2,
    files: Iterable[tuple[str, bytes, int, str]] = STANDARD_FILES,
) -> Path:
    """Create a populated RDB image whose every partition holds the standard files."""

    created: CreatedImage = create_hard_disc_image(
        directory,
        name=name,
        title="System",
        capacity=capacity,
        filesystem=filesystem,
        partitions=partitions,
    )
    wanted = tuple(files)
    with AmigaImage.open(created.path, writable=True) as image:
        for inode in image.children[ROOT_INODE]:
            populate(image, inode, wanted)
    return created.path


def create_hardfile(directory: Path, *, name: str = "hardfile", blocks: int = 8192) -> Path:
    """Create an RDB-less hardfile: one FFS volume starting at block zero."""

    from amigafs._vendor.amiganut.filesystem import reader_for
    from amigafs._vendor.amiganut.filesystem.amigados import format_volume

    path = directory / f"{name}.hdf"
    path.write_bytes(bytes(blocks * BLOCK))
    reader = reader_for(path, writable=True)
    try:
        format_volume(reader, label="Hardfile", dos_type=b"DOS\x03").flush()
    finally:
        reader.close()
    with AmigaImage.open(path, writable=True) as image:
        populate(image, ROOT_INODE, STANDARD_FILES)
    return path


def gzip_image(source: Path, destination: Path) -> Path:
    """Wrap a sector image as an ADZ or HDZ."""

    with source.open("rb") as raw, gzip.GzipFile(destination, "wb", mtime=0) as packed:
        packed.write(raw.read())
    return destination


def extended_adf(source: Path, destination: Path, *, raw_track: int | None = None) -> Path:
    """Wrap an ADF as a ``UAE-1ADF`` extended image, optionally with one raw MFM track."""

    data = source.read_bytes()
    track_bytes = 11 * BLOCK
    count = len(data) // track_bytes
    header = bytearray(b"UAE-1ADF")
    header += struct.pack(">HH", 0, count)
    body = bytearray()
    for index in range(count):
        kind = 1 if index == raw_track else 0
        header += struct.pack(">HHII", 0, kind, track_bytes, track_bytes * 8)
        body += data[index * track_bytes : (index + 1) * track_bytes]
    destination.write_bytes(bytes(header) + bytes(body))
    return destination


def rewrite_root(
    image_path: Path, offset_from_end: int, value: int, *, root: int = DD_ROOT_BLOCK
) -> None:
    """Change one long of a volume's root block and restore its checksum."""

    data = bytearray(image_path.read_bytes())
    start = root * BLOCK
    block = bytearray(data[start : start + BLOCK])
    struct.pack_into(">I", block, BLOCK - offset_from_end, value)
    data[start : start + BLOCK] = apply_checksum(block)
    image_path.write_bytes(data)


def invalidate_bitmap(image_path: Path) -> None:
    """Mark the allocation bitmap invalid, as an interrupted Amiga write does."""

    rewrite_root(image_path, 200, 0)


def scribble_bitmap(image_path: Path) -> None:
    """Mark a run of allocated blocks free in the first bitmap page."""

    data = bytearray(image_path.read_bytes())
    root = DD_ROOT_BLOCK * BLOCK
    (page_block,) = struct.unpack_from(">I", data, root + BLOCK - 196)
    start = page_block * BLOCK
    page = bytearray(data[start : start + BLOCK])
    page[4:200] = b"\xff" * 196
    data[start : start + BLOCK] = apply_checksum(page, 0)
    image_path.write_bytes(data)


def corrupt_file_header(image_path: Path, inner_path: str) -> None:
    """Break the checksum of one file's header block."""

    with AmigaImage.open(image_path) as image:
        block = image.mount_for(0).stat(inner_path).block
    data = bytearray(image_path.read_bytes())
    data[block * BLOCK + 30] ^= 0xFF
    image_path.write_bytes(data)


def tree(image: AmigaImage) -> dict[str, int | None]:
    """Return every indexed path with its size, or ``None`` for a drawer."""

    return {
        node.amiga_path: (None if node.is_dir else node.size)
        for node in image.nodes.values()
        if node.inode != ROOT_INODE
    }


def create_kickstart(directory: Path, *, name: str = "kick.rom", size: int = 262_144) -> Path:
    """Create a small valid ROM holding one resident module."""

    from amigafs._vendor.amiganut.kickfs.kickfs import build_rom

    path = directory / name
    path.write_bytes(
        build_rom(
            size=size,
            name="forge.library",
            id_string="forge.library 40.1 (1.1.93)",
            version=40,
            revision=1,
        )
    )
    return path
