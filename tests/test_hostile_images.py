"""Damaged and hostile images must be refused cleanly, never crash or hang.

These are deterministic: each case corrupts a valid image with a fixed seed.
The coverage-guided harness in ``fuzz/fuzz_volume.py`` explores further.
"""

from __future__ import annotations

import random
import signal
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from amigafs.core.image import AmigaImage
from amigafs.core.properties import read_image_properties
from amigafs.core.repair import plan_repairs
from amigafs.core.validation import validate_image_report
from amigafs.errors import AmigaFSError
from amigafs.recovery import pending_recovery
from tests.image_fixture import create_floppy, create_hard_disc

ROUNDS = 40


@contextmanager
def time_limit(seconds: int) -> Iterator[None]:
    def expired(_signal: int, _frame: object) -> None:
        raise AssertionError(f"a hostile image kept AmigaFS busy for over {seconds} seconds")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _exercise(path: Path) -> None:
    """Run every read-only entry point and one writable open."""

    with time_limit(30):
        report = validate_image_report(path)
        assert isinstance(report.findings, tuple)
        with suppress(AmigaFSError):
            read_image_properties(path)
        with suppress(AmigaFSError):
            plan_repairs(path)
        try:
            with AmigaImage.open(path) as image:
                for node in list(image.nodes.values())[:50]:
                    if not node.is_dir:
                        try:
                            image.read(node.inode, 0, 4096)
                        except Exception as exc:
                            assert not isinstance(exc, (MemoryError, RecursionError)), exc
        except AmigaFSError:
            pass
        with suppress(AmigaFSError):
            AmigaImage.open(path, writable=True).close(clean=False)


def _corrupt(data: bytes, generator: random.Random, *, regions: list[tuple[int, int]]) -> bytes:
    damaged = bytearray(data)
    for _ in range(generator.randint(1, 12)):
        start, length = generator.choice(regions)
        offset = start + generator.randrange(length)
        style = generator.randrange(4)
        if style == 0:
            damaged[offset] ^= 1 << generator.randrange(8)
        elif style == 1:
            damaged[offset : offset + 4] = generator.randbytes(4)
        elif style == 2:
            damaged[offset : offset + 4] = b"\xff\xff\xff\xff"
        else:
            damaged[offset : offset + 64] = bytes(64)
    return bytes(damaged[: len(data)])


@pytest.mark.parametrize("filesystem", ["OFS", "FFS", "FFS-DC", "FFS-LNFS"])
def test_corrupted_floppies_are_handled_without_crashing(tmp_path: Path, filesystem: str) -> None:
    source = create_floppy(tmp_path, filesystem=filesystem)
    original = source.read_bytes()
    # The boot block, and the root, bitmap, headers and data that surround it.
    regions = [(0, 1024), (870 * 512, 60 * 512)]
    generator = random.Random(f"floppy-{filesystem}")
    target = tmp_path / "hostile.adf"
    for _round in range(ROUNDS):
        target.write_bytes(_corrupt(original, generator, regions=regions))
        _exercise(target)
        assert pending_recovery(target) is None


@pytest.mark.parametrize("filesystem", ["FFS-INTL", "PFS3", "SFS"])
def test_corrupted_hard_discs_are_handled_without_crashing(tmp_path: Path, filesystem: str) -> None:
    source = create_hard_disc(tmp_path, filesystem=filesystem, capacity="4MB", partitions=2)
    original = source.read_bytes()
    with AmigaImage.open(source) as image:
        first = image.volumes[0]
    # The partition table, then the start and middle of the first partition.
    regions = [
        (0, 4 * 512),
        (first.offset, 64 * 512),
        (first.offset + first.length // 2 - 16 * 512, 64 * 512),
    ]
    generator = random.Random(f"disc-{filesystem}")
    target = tmp_path / "hostile.hdf"
    for _round in range(ROUNDS):
        target.write_bytes(_corrupt(original, generator, regions=regions))
        _exercise(target)
        assert pending_recovery(target) is None


def test_directory_cycle_is_refused(tmp_path: Path) -> None:
    from amigafs._vendor.amiganut.filesystem.blocks import apply_checksum

    source = create_floppy(tmp_path)
    with AmigaImage.open(source) as image:
        mount = image.mount_for(0)
        docs = mount.stat("Docs").block
        nested = mount.stat("Docs/Deep/Nested").block
    data = bytearray(source.read_bytes())
    # Make the innermost drawer's hash table point back at an ancestor.
    block = bytearray(data[nested * 512 : (nested + 1) * 512])
    block[24:28] = docs.to_bytes(4, "big")
    data[nested * 512 : (nested + 1) * 512] = apply_checksum(block)
    source.write_bytes(data)
    with time_limit(30):
        with pytest.raises(AmigaFSError, match="cycle|damaged|cannot be read"):
            AmigaImage.open(source)
        report = validate_image_report(source)
    assert [finding.code for finding in report.findings] == ["image.open_failed"]


def test_truncated_and_oversized_images_are_refused_or_bounded(tmp_path: Path) -> None:
    source = create_floppy(tmp_path)
    original = source.read_bytes()
    for size in (512, 1024, 450_560, 901_120 - 512):
        target = tmp_path / f"short-{size}.adf"
        target.write_bytes(original[:size])
        _exercise(target)
    padded = tmp_path / "padded.adf"
    padded.write_bytes(original + bytes(512 * 64))
    _exercise(padded)


def test_partition_table_loops_and_wild_geometry_are_refused(tmp_path: Path) -> None:
    source = create_hard_disc(tmp_path, capacity="4MB", partitions=2)
    original = bytearray(source.read_bytes())

    def resealed(data: bytearray, block: int) -> bytearray:
        start = block * 512
        data[start + 8 : start + 12] = bytes(4)
        total = sum(
            int.from_bytes(data[start + index : start + index + 4], "big")
            for index in range(0, 256, 4)
        )
        data[start + 8 : start + 12] = ((-total) & 0xFFFFFFFF).to_bytes(4, "big")
        return data

    looped = bytearray(original)
    # The second partition entry names the first as its successor.
    looped[2 * 512 + 16 : 2 * 512 + 20] = (1).to_bytes(4, "big")
    wild = bytearray(original)
    wild[512 + 128 + 12 : 512 + 128 + 16] = (0xFFFFFFFF).to_bytes(4, "big")
    wild[512 + 128 + 40 : 512 + 128 + 44] = (0xFFFFFFF0).to_bytes(4, "big")
    zero = bytearray(original)
    zero[512 + 128 + 20 : 512 + 128 + 24] = bytes(4)
    for name, data, block in (("loop", looped, 2), ("wild", wild, 1), ("zero", zero, 1)):
        target = tmp_path / f"{name}.hdf"
        target.write_bytes(resealed(data, block))
        _exercise(target)
