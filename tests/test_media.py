"""Physical discs, exercised through an ordinary file adopted as a device."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from amigafs.core import media
from amigafs.core.formats import ImageCapabilities, ResolvedImage
from amigafs.core.image import ROOT_INODE, AmigaImage
from amigafs.core.media import detect_volumes, open_media
from amigafs.errors import AmigaFSError, UnsupportedImageError
from amigafs.recovery import pending_recovery, recover_image
from tests.image_fixture import create_floppy, create_hard_disc, create_hardfile

CAPABILITIES = ImageCapabilities(True, True, True, True, True, True, False, False)


def _as_disc(path: Path, monkeypatch: pytest.MonkeyPatch) -> ResolvedImage:
    """Present an image file as a physical disc whose descriptor is handed over."""

    opened: list[bool] = []

    def open_device(_selected: object, *, writable: bool, **_kwargs: object) -> int:
        opened.append(writable)
        return os.open(path, os.O_RDWR if writable else os.O_RDONLY)

    monkeypatch.setattr("amigafs.core.devices.open_device", open_device)
    # A regular file is not a block device, so the adopted descriptor is allowed
    # through exactly as a real disc's would be.
    return ResolvedImage(
        primary_path=path,
        kind="physical-disc",
        capabilities=CAPABILITIES,
        is_device=True,
        display_name="Pretend CF card",
    )


def test_physical_disc_is_opened_from_a_descriptor_and_journalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = create_hard_disc(tmp_path, filesystem="PFS3", capacity="8MB", partitions=2)
    original = path.read_bytes()
    source = _as_disc(path, monkeypatch)
    with AmigaImage.open(source, writable=True) as image:
        assert image.source.name == "Pretend CF card"
        assert image.layout == "rdb"
        info = pending_recovery(path)
        assert info is not None and info.kind == "journal"
        node = image.create_file(image.children[ROOT_INODE][1], b"OnTheCard")
        image.replace_file(node.inode, b"written to a physical disc")
    assert pending_recovery(path) is None
    assert path.read_bytes() != original
    with AmigaImage.open(_as_disc(path, monkeypatch)) as image:
        assert image.read(image.node_at_path("DH1:OnTheCard").inode, 0, 99) == (
            b"written to a physical disc"
        )
        assert image.integrity_report().findings == ()


def test_interrupted_physical_disc_session_is_restored_block_for_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = create_hard_disc(tmp_path, capacity="8MB", partitions=1)
    original = path.read_bytes()
    image = AmigaImage.open(_as_disc(path, monkeypatch), writable=True)
    top = image.children[ROOT_INODE][0]
    image.make_directory(top, b"Interrupted")
    image.store.handle.close()
    assert path.read_bytes() != original
    recover_image(path, restore=True)
    assert path.read_bytes() == original


def test_every_supported_layout_is_detected(tmp_path: Path) -> None:
    for creator, layout, count in (
        (lambda: create_floppy(tmp_path), "single", 1),
        (lambda: create_hardfile(tmp_path), "single", 1),
        (lambda: create_hard_disc(tmp_path, capacity="8MB", partitions=3), "rdb", 3),
    ):
        opened = open_media(creator())
        try:
            assert opened.layout == layout
            assert len(opened.volumes) == count
            assert all(volume.mountable for volume in opened.volumes)
            if layout == "rdb":
                assert opened.rigid_disk is not None
                assert [volume.directory for volume in opened.volumes] == ["DH0", "DH1", "DH2"]
                assert (
                    opened.volumes[1].offset == opened.volumes[0].offset + opened.volumes[0].length
                )
        finally:
            opened.close()


def test_partition_names_become_safe_unique_folder_names() -> None:
    taken: set[str] = set()
    names = [
        media._directory_name(name, index, taken)
        for index, name in enumerate(["DH0", "dh0", "a/b", "", "..", "Work\x07", "DH0"])
    ]
    assert names == ["DH0", "dh0-2", "a_b", "partition3", "partition4", "Work_", "DH0-3"]


def test_partition_beyond_the_medium_is_skipped_not_trusted(tmp_path: Path) -> None:
    path = create_hard_disc(tmp_path, capacity="8MB", partitions=2)
    truncated = tmp_path / "truncated.hdf"
    truncated.write_bytes(path.read_bytes()[: 5 * 1024 * 1024])
    with AmigaImage.open(truncated) as image:
        assert image.mounted_volumes == (0,)
        ((volume, reason),) = image.unmounted
        assert volume.device_name == "DH1"
        assert "beyond the end of the medium" in reason
    # The intact partition can still be written; the missing one cannot exist.
    with AmigaImage.open(truncated, writable=True) as image:
        image.create_file(image.children[ROOT_INODE][0], b"StillUsable")
        assert len(image.children[ROOT_INODE]) == 1


def test_damaged_partition_table_is_refused(tmp_path: Path) -> None:
    path = create_hard_disc(tmp_path, capacity="4MB")
    data = bytearray(path.read_bytes())
    data[8] ^= 0xFF
    path.write_bytes(data)
    with pytest.raises(AmigaFSError, match="Rigid Disk Block cannot be read"):
        AmigaImage.open(path)


def test_geometry_sidecar_is_bounded_and_optional(tmp_path: Path) -> None:
    path = create_hardfile(tmp_path)
    assert media._geometry_sidecar(path) is None
    sidecar = path.with_suffix(".hdf.geo")
    sidecar.write_text("surfaces=1\nblockspertrack=32\nreserved=2\n", encoding="utf-8")
    geometry = media._geometry_sidecar(path)
    assert geometry is not None and geometry.blocks_per_track == 32
    sidecar.write_text("x" * 5000, encoding="utf-8")
    assert media._geometry_sidecar(path) is None
    sidecar.write_text("nothing useful here", encoding="utf-8")
    assert media._geometry_sidecar(path) is None
    with AmigaImage.open(path) as image:
        assert image.integrity_report().findings == ()


def test_empty_and_unrecognised_media_are_refused(tmp_path: Path) -> None:
    from amigafs.core.blockio import ImageStore

    blank = tmp_path / "blank.bin"
    blank.write_bytes(bytes(8192))
    source = ResolvedImage(primary_path=blank, kind="hard-disc-image", capabilities=CAPABILITIES)
    store = ImageStore.open(blank, writable=False)
    try:
        with pytest.raises(UnsupportedImageError):
            detect_volumes(store, source, writable=False)
    finally:
        store.close()
