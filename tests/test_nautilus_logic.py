from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from amigafs.core import read_image_properties, resolve_image
from amigafs_nautilus.logic import (
    MAX_PROPERTIES_DECODE_BYTES,
    decodes_quickly,
    image_capabilities,
    image_property_rows,
    is_supported_image,
    menu_capabilities,
    mounted_file_property_rows,
    summary_property_rows,
)
from tests.image_fixture import (
    create_floppy,
    create_hard_disc,
    extended_adf,
    gzip_image,
    invalidate_bitmap,
)


def test_floppy_and_hard_disc_images_offer_every_protected_action(tmp_path: Path) -> None:
    for path in (create_floppy(tmp_path), create_hard_disc(tmp_path, capacity="4MB")):
        capabilities = menu_capabilities(path)
        assert capabilities is not None
        assert capabilities.mount_read_only and capabilities.mount_read_write
        assert capabilities.validate and capabilities.repair and capabilities.recover
        assert is_supported_image(path)


def test_read_only_formats_offer_only_safe_actions(tmp_path: Path) -> None:
    source = create_floppy(tmp_path)
    wrapped = extended_adf(source, tmp_path / "wrapped.adf")
    capabilities = menu_capabilities(wrapped)
    assert capabilities is not None
    assert capabilities.mount_read_only
    assert not capabilities.mount_read_write
    assert not capabilities.repair and not capabilities.recover
    packed = menu_capabilities(gzip_image(source, tmp_path / "disk.adz"))
    assert packed is not None and packed.mount_read_write and not packed.repair


def test_files_from_other_systems_with_a_shared_suffix_are_ignored(tmp_path: Path) -> None:
    acorn = tmp_path / "acorn.adf"
    data = bytearray(800 * 1024)
    data[0x201:0x205] = b"Hugo"
    acorn.write_bytes(data)
    rom = tmp_path / "bios.rom"
    rom.write_bytes(b"\x55\xaa" + bytes(65534))
    assert menu_capabilities(acorn) is None
    assert menu_capabilities(rom) is None
    assert not is_supported_image(tmp_path / "missing.adf")


def test_ordinary_files_are_never_opened(tmp_path: Path) -> None:
    document = tmp_path / "notes.txt"
    document.write_text("hello", encoding="utf-8")
    generic = tmp_path / "disk.img"
    generic.write_bytes(create_floppy(tmp_path).read_bytes())
    with patch("amigafs_nautilus.logic.resolve_image", side_effect=AssertionError("opened")):
        assert menu_capabilities(document) is None
        # A generic suffix is never claimed on the desktop, whatever it holds.
        assert menu_capabilities(generic) is None
    # It can still be opened when asked for by name.
    assert image_capabilities(generic) is not None


def test_large_containers_are_described_without_being_decoded(tmp_path: Path) -> None:
    small = resolve_image(gzip_image(create_floppy(tmp_path), tmp_path / "disk.adz"))
    assert decodes_quickly(small)
    assert decodes_quickly(resolve_image(create_hard_disc(tmp_path, capacity="4MB")))
    assert not decodes_quickly(resolve_image("floppy:A"))
    big = tmp_path / "big.hdz"
    with big.open("wb") as handle:
        handle.write(b"\x1f\x8b\x08\x00")
        handle.truncate(MAX_PROPERTIES_DECODE_BYTES + 1)
    source = resolve_image(big)
    assert not decodes_quickly(source)
    rows = dict(summary_property_rows(source))
    assert rows["Image type"] == "Compressed Amiga image"
    assert rows["Container"] == "GZIP"
    assert rows["Read-write mounting"] == "Supported"


def test_floppy_property_rows(tmp_path: Path) -> None:
    rows = dict(image_property_rows(read_image_properties(create_floppy(tmp_path))))
    assert rows["Image type"] == "Amiga floppy image (ADF)"
    assert rows["Layout"] == "One volume, no partition table"
    assert rows["Geometry"] == "80 cylinders × 2 heads × 11 sectors/track"
    assert rows["Capacity"] == "880.0 KiB"
    assert rows["Filesystem"] == "FFS (DOS\\1)"
    assert rows["Volume name"] == "Workbench"
    assert rows["Contents"] == "5 files, 5 drawers"
    assert rows["Validation"] == "Safe for read-write mounting"
    assert "Container" not in rows and "Drive identity" not in rows


def test_hard_disc_property_rows_are_grouped_by_partition(tmp_path: Path) -> None:
    path = create_hard_disc(
        tmp_path, filesystem="PFS3", capacity="8MB", files=(("Only", b"x", 0, ""),)
    )
    rows = dict(image_property_rows(read_image_properties(path)))
    assert rows["Drive identity"] == "AMIGA AMIGAFS HDF 1.1"
    assert rows["DH0: Filesystem"] == "PFS3 (PFS\\3)"
    assert rows["DH0: Volume name"] == "System"
    assert rows["DH1: Volume name"] == "System1"
    assert rows["DH0: Boot priority"] == "0"
    assert "DH1: Boot priority" not in rows
    assert rows["DH0: Contents"] == "1 file, 0 drawers"
    assert rows["DH0: Cylinders"].startswith("1–")


def test_property_rows_report_problems_and_read_only_formats(tmp_path: Path) -> None:
    damaged = create_floppy(tmp_path)
    invalidate_bitmap(damaged)
    rows = dict(image_property_rows(read_image_properties(damaged)))
    assert rows["Validation"].startswith("Unsafe for read-write mounting (")
    assert "problem" in rows["Validation"]
    wrapped = extended_adf(create_floppy(tmp_path, name="clean"), tmp_path / "wrapped.adf")
    rows = dict(image_property_rows(read_image_properties(wrapped)))
    assert rows["Validation"] == "Supported read-only"
    assert rows["Container"] == "UAE extended ADF"


def test_mounted_file_properties_are_derived_from_amiga_xattrs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {
        "user.amiga.source": b"FFS-INTL",
        "user.amiga.path": "DH0:Texte/Größe".encode(),
        "user.amiga.volume": b"Workbench",
        "user.amiga.protection": b"-s--rwed",
        "user.amiga.comment": "Ein Kommentar ©".encode(),
    }

    def getxattr(_path: str, name: str) -> bytes:
        try:
            return values[name]
        except KeyError as exc:
            raise OSError(61, "no data") from exc

    monkeypatch.setattr(os, "getxattr", getxattr)
    assert mounted_file_property_rows(tmp_path / "file") == (
        ("Source filesystem", "FFS-INTL"),
        ("Amiga path", "DH0:Texte/Größe"),
        ("Volume name", "Workbench"),
        ("Protection bits", "-s--rwed"),
        ("Comment", "Ein Kommentar ©"),
    )
    values.clear()
    assert mounted_file_property_rows(tmp_path / "file") == ()
