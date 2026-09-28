from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from amigafs.core.formats import (
    DESKTOP_SUFFIXES,
    floppy_drive_for_path,
    floppy_reference,
    image_capabilities_hint,
    resolve_image,
)
from amigafs.errors import UnsupportedImageError
from tests.image_fixture import (
    create_floppy,
    create_hard_disc,
    create_hardfile,
    extended_adf,
    gzip_image,
)


def test_detects_floppy_images_from_content_not_extension(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    renamed = tmp_path / "workbench.bin"
    shutil.copyfile(path, renamed)
    for candidate in (path, renamed):
        image = resolve_image(candidate)
        assert image.kind == "floppy-image"
        assert image.container is None
        assert image.capabilities.mount_read_write
        assert image.capabilities.repair
        assert image.capabilities.write_floppy
        assert image.primary_path == candidate.resolve()


def test_detects_partitioned_and_unpartitioned_hard_discs(tmp_path: Path) -> None:
    for path in (create_hard_disc(tmp_path, capacity="4MB"), create_hardfile(tmp_path)):
        image = resolve_image(path)
        assert image.kind == "hard-disc-image"
        assert image.capabilities.mount_read_write
        assert not image.capabilities.write_floppy


def test_rigid_disk_block_is_found_in_any_of_the_first_sixteen_blocks(tmp_path: Path) -> None:
    path = tmp_path / "late.hdf"
    path.write_bytes(bytes(15 * 512) + b"RDSK" + bytes(64 * 512 - 4))
    assert resolve_image(path).kind == "hard-disc-image"
    path.write_bytes(bytes(16 * 512) + b"RDSK" + bytes(64 * 512 - 4))
    with pytest.raises(UnsupportedImageError):
        resolve_image(path)


@pytest.mark.parametrize(
    ("magic", "container", "kind", "writable"),
    [
        (b"\x1f\x8b\x08\x00", "gzip", "compressed-image", True),
        (b"DMS!", "dms", "dms-archive", False),
        (b"UAE-1ADF", "extended-adf", "extended-adf", False),
        (b"UAE--ADF", "extended-adf", "extended-adf", False),
        (b"HXCPICFE", "hfe", "flux-image", True),
        (b"HXCHFEV3", "hfe", "flux-image", True),
        (b"SCP\x19", "scp", "flux-image", False),
        (b"CAPS", "ipf", "flux-image", False),
    ],
)
def test_containers_are_recognised_from_their_header_without_decoding(
    tmp_path: Path, magic: bytes, container: str, kind: str, writable: bool
) -> None:
    path = tmp_path / "anything.dat"
    path.write_bytes(magic + bytes(2048))
    image = resolve_image(path)
    assert (image.container, image.kind) == (container, kind)
    assert image.capabilities.mount_read_only
    assert image.capabilities.mount_read_write is writable
    assert not image.capabilities.repair


def test_real_compressed_and_extended_images_resolve(tmp_path: Path) -> None:
    source = create_floppy(tmp_path)
    assert resolve_image(gzip_image(source, tmp_path / "disk.adz")).container == "gzip"
    assert resolve_image(extended_adf(source, tmp_path / "ext.adf")).container == "extended-adf"


def test_acorn_and_other_foreign_images_are_rejected(tmp_path: Path) -> None:
    acorn = tmp_path / "acorn.adf"
    data = bytearray(800 * 1024)
    data[0x201:0x205] = b"Hugo"
    acorn.write_bytes(data)
    fat = tmp_path / "pc.img"
    fat.write_bytes(b"\xeb\x3c\x90MSDOS5.0" + bytes(1440 * 1024 - 11))
    empty = tmp_path / "empty.adf"
    empty.write_bytes(b"")
    ragged = tmp_path / "ragged.adf"
    ragged.write_bytes(b"DOS\x00" + bytes(1000))
    future = tmp_path / "future.adf"
    future.write_bytes(b"DOS\x08" + bytes(901120 - 4))
    for path in (acorn, fat, empty, ragged, future):
        with pytest.raises(UnsupportedImageError):
            resolve_image(path)


def test_missing_and_special_files_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedImageError):
        resolve_image(tmp_path / "missing.adf")
    with pytest.raises(UnsupportedImageError):
        resolve_image(tmp_path)
    fifo = tmp_path / "fifo.adf"
    os.mkfifo(fifo)
    with pytest.raises(UnsupportedImageError):
        resolve_image(fifo)


def test_floppy_references_name_a_drive_and_resolve_to_a_private_token(tmp_path: Path) -> None:
    assert floppy_reference("floppy:a") == "A"
    assert floppy_reference("greaseweazle:2") == "2"
    assert floppy_reference("floppy:C") is None
    assert floppy_reference("floppy:AB") is None
    assert floppy_reference(Path("floppy:A")) is None
    image = resolve_image("floppy:b")
    assert image.kind == "physical-floppy"
    assert image.container == "greaseweazle"
    assert image.drive == "B"
    assert image.name == "Floppy drive B"
    assert image.primary_path.name == "drive-B"
    assert image.primary_path.stat().st_mode & 0o777 == 0o600
    assert floppy_drive_for_path(image.primary_path) == "B"
    # The token resolves back to the same drive, which is how recovery finds it.
    assert resolve_image(image.primary_path).drive == "B"
    impostor = tmp_path / "floppy" / "drive-A"
    impostor.parent.mkdir()
    impostor.write_bytes(b"")
    assert floppy_drive_for_path(impostor) is None


def test_suffix_hints_cover_the_desktop_suffixes_and_nothing_generic() -> None:
    for suffix in DESKTOP_SUFFIXES:
        assert image_capabilities_hint(f"disk{suffix}") is not None
        assert image_capabilities_hint(f"DISK{suffix.upper()}") is not None
    for name in ("disk.img", "disk.raw", "disk.iso", "notes.txt", "scsi0.dat"):
        assert image_capabilities_hint(name) is None
    assert image_capabilities_hint("game.dms").mount_read_write is False  # type: ignore[union-attr]
    assert image_capabilities_hint("kick31.rom").mount_read_write is False  # type: ignore[union-attr]


def test_resolution_never_decodes_or_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = create_floppy(tmp_path)
    packed = gzip_image(source, tmp_path / "disk.adz")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("resolution must not decode a container")

    monkeypatch.setattr("amigafs.core.containers.decode_container", forbidden)
    monkeypatch.setattr("amigafs.core.containers.run_greaseweazle", forbidden)
    monkeypatch.setattr("gzip.open", forbidden)
    assert resolve_image(packed).container == "gzip"
    from amigafs.core.blockio import ImageStore

    held = ImageStore.open(source, writable=True)
    try:
        assert resolve_image(source).kind == "floppy-image"
    finally:
        held.close()
