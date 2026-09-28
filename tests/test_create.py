from __future__ import annotations

from pathlib import Path

import pytest

from amigafs.core.create import (
    FLOPPY_FILESYSTEMS,
    create_floppy_image,
    create_hard_disc_image,
    normalise_filesystem,
    parse_capacity,
)
from amigafs.core.image import ROOT_INODE, AmigaImage
from amigafs.core.validation import validate_image_report
from amigafs.errors import AmigaFSError


@pytest.mark.parametrize("filesystem", FLOPPY_FILESYSTEMS)
@pytest.mark.parametrize("density", ["dd", "hd"])
def test_every_floppy_variant_is_created_valid(
    tmp_path: Path, filesystem: str, density: str
) -> None:
    updates: list[tuple[int, str]] = []
    created = create_floppy_image(
        tmp_path,
        name="blank",
        title="Größe",
        density=density,
        filesystem=filesystem,
        bootable=True,
        progress=lambda percent, text: updates.append((percent, text)),
    )
    assert created.path == tmp_path / "blank.adf"
    assert created.capacity_bytes == (901_120 if density == "dd" else 1_802_240)
    assert (created.title, created.filesystem) == ("Größe", filesystem)
    assert [percent for percent, _text in updates] == [0, 10, 70, 90, 100]
    assert not [child for child in tmp_path.iterdir() if child.name.startswith(".")]
    with AmigaImage.open(created.path, writable=True) as image:
        assert image.volume_title(0) == "Größe"
        assert image.volumes[0].format == filesystem
        image.create_file(ROOT_INODE, b"Works")
    assert validate_image_report(created.path).findings == ()


def test_bootable_floppy_carries_a_checksummed_boot_block(tmp_path: Path) -> None:
    plain = create_floppy_image(tmp_path, name="plain").path.read_bytes()
    bootable = create_floppy_image(tmp_path, name="boot", bootable=True).path.read_bytes()
    assert plain[:4] == bootable[:4] == b"DOS\x00"
    # Both carry a checksum and root pointer; only a bootable disk carries code.
    assert plain[12:1024] == bytes(1012)
    assert bootable[12:1024] != bytes(1012)
    total = 0
    for offset in range(0, 1024, 4):
        total += int.from_bytes(bootable[offset : offset + 4], "big")
        if total > 0xFFFFFFFF:
            total = (total & 0xFFFFFFFF) + 1
    assert total == 0xFFFFFFFF


@pytest.mark.parametrize(
    ("filesystem", "capacity", "partitions"),
    [("FFS-INTL", "40MB", 1), ("FFS", "90MB", 3), ("PFS3", "64MB", 2), ("SFS", "64MB", 2)],
)
def test_hard_disc_images_are_partitioned_formatted_and_valid(
    tmp_path: Path, filesystem: str, capacity: str, partitions: int
) -> None:
    created = create_hard_disc_image(
        tmp_path,
        name="disc",
        title="System",
        capacity=capacity,
        filesystem=filesystem,
        partitions=partitions,
    )
    assert created.path == tmp_path / "disc.hdf"
    assert created.partitions == tuple(f"DH{index}" for index in range(partitions))
    assert created.capacity_bytes % (16 * 63 * 512) == 0
    assert created.path.read_bytes()[:4] == b"RDSK"
    report = validate_image_report(created.path)
    assert report.findings == ()
    assert len(report.volumes) == partitions
    with AmigaImage.open(created.path) as image:
        assert image.volume_title(0) == "System"
        assert image.volumes[0].bootable
        assert all(not image.volumes[index].bootable for index in range(1, partitions))


def test_a_partition_needing_bitmap_extension_blocks_is_created_valid(tmp_path: Path) -> None:
    created = create_hard_disc_image(
        tmp_path, name="large", capacity="200MB", filesystem="FFS", partitions=1
    )
    assert validate_image_report(created.path).findings == ()
    payload = bytes(range(256)) * 4096
    with AmigaImage.open(created.path, writable=True) as image:
        drawer = image.lookup(ROOT_INODE, b"DH0")
        assert drawer is not None
        node = image.create_file(drawer.inode, b"payload.bin")
        image.replace_file(node.inode, payload)
    with AmigaImage.open(created.path) as image:
        drawer = image.lookup(ROOT_INODE, b"DH0")
        assert drawer is not None
        found = image.lookup(drawer.inode, b"payload.bin")
        assert found is not None
        assert image.read(found.inode, 0, len(payload)) == payload
    assert validate_image_report(created.path).findings == ()


def test_created_images_never_overwrite_an_existing_file(tmp_path: Path) -> None:
    (tmp_path / "BLANK.ADF").write_bytes(b"precious")
    with pytest.raises(AmigaFSError, match="would overwrite existing file: BLANK.ADF"):
        create_floppy_image(tmp_path, name="blank")
    assert (tmp_path / "BLANK.ADF").read_bytes() == b"precious"
    (tmp_path / "harddisk.hdf").write_bytes(b"precious")
    with pytest.raises(AmigaFSError, match="would overwrite"):
        create_hard_disc_image(tmp_path)
    assert sorted(child.name for child in tmp_path.iterdir()) == ["BLANK.ADF", "harddisk.hdf"]


@pytest.mark.parametrize(
    "settings",
    [
        {"name": "../escape"},
        {"name": "with space"},
        {"name": "x" * 70},
        {"title": "bad:title"},
        {"title": "T" * 31},
        {"title": "snow☃"},
        {"density": "qd"},
        {"filesystem": "PFS3"},
        {"filesystem": "NTFS"},
    ],
)
def test_invalid_floppy_settings_are_rejected(tmp_path: Path, settings: dict[str, str]) -> None:
    with pytest.raises(AmigaFSError):
        create_floppy_image(tmp_path, **settings)  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "settings",
    [
        {"capacity": "1MB"},
        {"capacity": "100GB"},
        {"capacity": "lots"},
        {"partitions": 0},
        {"partitions": 17},
        {"capacity": "2MB", "partitions": 8},
        {"filesystem": "EXT4"},
    ],
)
def test_invalid_hard_disc_settings_are_rejected(
    tmp_path: Path, settings: dict[str, object]
) -> None:
    with pytest.raises(AmigaFSError):
        create_hard_disc_image(tmp_path, **settings)  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


def test_destination_must_be_a_directory(tmp_path: Path) -> None:
    with pytest.raises(AmigaFSError, match="not a directory"):
        create_floppy_image(tmp_path / "missing")


def test_an_image_that_fails_validation_is_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amigafs.core.validation import FindingSeverity, IntegrityFinding, IntegrityReport

    broken = IntegrityReport(
        image_path="",
        image_bytes=0,
        image_kind="floppy-image",
        layout="single",
        volumes=(),
        findings=(IntegrityFinding(FindingSeverity.FATAL, "volume.structure", "made up"),),
    )
    monkeypatch.setattr("amigafs.core.create.validate_image_report", lambda _path: broken)
    with pytest.raises(AmigaFSError, match="failed validation: volume.structure: made up"):
        create_floppy_image(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_capacity_and_filesystem_parsing() -> None:
    assert parse_capacity("40MB") == 40 * 1024 * 1024
    assert parse_capacity(" 1.5 g ") == 1536 * 1024 * 1024
    assert parse_capacity("880k") == 880 * 1024
    assert parse_capacity("1000") == 512
    assert parse_capacity("64MiB") == 64 * 1024 * 1024
    assert normalise_filesystem("ffs_intl", allowed=("FFS-INTL",)) == "FFS-INTL"
    assert normalise_filesystem("pfs", allowed=("PFS3",)) == "PFS3"
    assert normalise_filesystem("", allowed=("FFS",)) == "FFS"
    for bad in ("", "MB", "-4MB", "4XB"):
        with pytest.raises(AmigaFSError):
            parse_capacity(bad)
