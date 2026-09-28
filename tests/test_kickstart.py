from __future__ import annotations

from pathlib import Path

import pytest

from amigafs.core.formats import resolve_image
from amigafs.core.image import ROOT_INODE, AmigaImage
from amigafs.core.properties import read_image_properties
from amigafs.core.validation import validate_image_report
from amigafs.errors import AmigaFSError, UnsupportedImageError
from amigafs.recovery import pending_recovery
from tests.image_fixture import create_kickstart


@pytest.mark.parametrize("size", [262_144, 524_288])
def test_kickstart_rom_is_recognised_from_content(tmp_path: Path, size: int) -> None:
    rom = create_kickstart(tmp_path, name="anything.bin", size=size)
    image = resolve_image(rom)
    assert image.kind == "kickstart-rom"
    assert image.case_sensitive_names
    assert image.capabilities.mount_read_only
    assert not image.capabilities.mount_read_write
    assert not image.capabilities.repair and not image.capabilities.recover
    assert not image.capabilities.write_floppy


def test_kickstart_modules_are_listed_and_readable(tmp_path: Path) -> None:
    rom = create_kickstart(tmp_path)
    before = rom.read_bytes()
    with AmigaImage.open(rom) as image:
        assert image.layout == "kickstart"
        (inode,) = image.children[ROOT_INODE]
        module = image.nodes[inode]
        assert module.name == b"forge.library"
        assert not module.is_dir
        assert module.comment == "forge.library 40.1 (1.1.93)"
        assert module.write_protected and module.delete_protected
        data = image.read(inode, 0, module.size)
        assert len(data) == module.size > 0
        assert data in before
        # Module names are case-sensitive: a ROM is not an AmigaDOS volume.
        assert image.lookup(ROOT_INODE, b"FORGE.LIBRARY") is None
        assert image.total_bytes == len(before)
        with pytest.raises(PermissionError):
            image.create_file(ROOT_INODE, b"new.library")
    assert rom.read_bytes() == before


def test_kickstart_is_never_writable(tmp_path: Path) -> None:
    rom = create_kickstart(tmp_path)
    with pytest.raises(AmigaFSError, match="not supported for this image format"):
        AmigaImage.open(rom, writable=True)
    assert pending_recovery(rom) is None


def test_kickstart_validation_and_properties(tmp_path: Path) -> None:
    rom = create_kickstart(tmp_path)
    report = validate_image_report(rom)
    assert report.findings == ()
    assert report.layout == "kickstart"
    properties = read_image_properties(rom)
    assert properties.image_type == "Kickstart ROM"
    assert properties.layout_label == "Resident module list"
    assert not properties.read_write_supported
    (volume,) = properties.volumes
    assert (volume.filesystem, volume.files, volume.writable) == ("kickfs", 1, False)
    damaged = bytearray(rom.read_bytes())
    damaged[0x8000] ^= 0xFF
    rom.write_bytes(damaged)
    report = validate_image_report(rom)
    assert [finding.code for finding in report.findings] == ["volume.structure"]
    assert "checksum" in report.findings[0].message


def test_files_that_only_resemble_a_rom_are_rejected(tmp_path: Path) -> None:
    pc_bios = tmp_path / "bios.rom"
    pc_bios.write_bytes(b"\x55\xaa" + bytes(65534))
    tiny = tmp_path / "tiny.rom"
    tiny.write_bytes(b"\x11\x14\x4e\xf9" + bytes(100))
    huge = tmp_path / "huge.rom"
    with huge.open("wb") as handle:
        handle.write(b"\x11\x14\x4e\xf9")
        handle.truncate(32 * 1024 * 1024)
    for path in (pc_bios, tiny, huge):
        with pytest.raises(UnsupportedImageError):
            resolve_image(path)
