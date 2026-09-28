#!/usr/bin/env python3
"""Coverage-guided target for volume detection, indexing and validation.

The input is applied as a series of patches to a small valid image, so the
fuzzer spends its time in the directory, bitmap and partition code rather than
being rejected at the first signature check.
"""

import sys
import tempfile
from pathlib import Path

import atheris

with atheris.instrument_imports():
    from amigafs.core.create import create_floppy_image, create_hard_disc_image
    from amigafs.core.image import ROOT_INODE, AmigaImage
    from amigafs.core.validation import validate_image_report
    from amigafs.errors import AmigaFSError

_TEMPORARY = tempfile.TemporaryDirectory(prefix="amigafs-fuzz-")
_ROOT = Path(_TEMPORARY.name)
_SEEDS: list[bytes] = []
_TARGET = _ROOT / "fuzz.img"


def _seeds() -> list[bytes]:
    if not _SEEDS:
        floppy = create_floppy_image(_ROOT, name="seed-floppy", filesystem="FFS-DC").path
        with AmigaImage.open(floppy, writable=True) as image:
            drawer = image.make_directory(ROOT_INODE, b"Drawer")
            node = image.create_file(drawer.inode, b"File")
            image.replace_file(node.inode, b"seed data" * 200)
        _SEEDS.append(floppy.read_bytes())
        for filesystem in ("PFS3", "SFS"):
            disc = create_hard_disc_image(
                _ROOT, name=f"seed-{filesystem}", capacity="4MB", filesystem=filesystem
            ).path
            _SEEDS.append(disc.read_bytes())
    return _SEEDS


def test_one_input(data: bytes) -> None:
    if len(data) < 2:
        return
    seeds = _seeds()
    image = bytearray(seeds[data[0] % len(seeds)])
    cursor = 1
    while cursor + 6 <= len(data):
        offset = int.from_bytes(data[cursor : cursor + 4], "big") % len(image)
        length = data[cursor + 4] % 32
        patch = data[cursor + 5 : cursor + 5 + length]
        image[offset : offset + len(patch)] = patch
        cursor += 5 + max(1, length)
    _TARGET.write_bytes(bytes(image[: len(seeds[data[0] % len(seeds)])]))
    try:
        validate_image_report(_TARGET)
    except AmigaFSError:
        return


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
