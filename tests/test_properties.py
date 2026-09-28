from __future__ import annotations

import json
from pathlib import Path

from amigafs.core.properties import dos_type_text, kind_label, read_image_properties
from tests.image_fixture import (
    create_floppy,
    create_hard_disc,
    create_hardfile,
    gzip_image,
    invalidate_bitmap,
)


def test_floppy_properties_report_format_geometry_and_validation(tmp_path: Path) -> None:
    properties = read_image_properties(create_floppy(tmp_path, filesystem="FFS-INTL"))
    assert properties.image_kind == "floppy-image"
    assert properties.image_type == "Amiga floppy image (ADF)"
    assert properties.container == ""
    assert properties.layout == "single"
    assert (properties.cylinders, properties.heads, properties.sectors_per_track) == (80, 2, 11)
    assert properties.capacity_bytes == 901_120
    assert properties.read_write_supported
    assert properties.validation_state == "passed"
    assert properties.fatal_findings == properties.warning_findings == 0
    (volume,) = properties.volumes
    assert volume.title == "Workbench"
    assert (volume.format, volume.dos_type, volume.filesystem) == ("FFS-INTL", "DOS\\3", "ffs")
    assert (volume.files, volume.directories) == (5, 5)
    assert volume.mounted and volume.writable
    assert volume.first_cylinder is None
    assert volume.used_bytes is not None and volume.free_bytes is not None
    assert volume.used_bytes + volume.free_bytes == volume.capacity_bytes


def test_high_density_floppy_geometry(tmp_path: Path) -> None:
    properties = read_image_properties(create_floppy(tmp_path, density="hd"))
    assert (properties.cylinders, properties.heads, properties.sectors_per_track) == (80, 2, 22)


def test_hard_disc_properties_describe_the_drive_and_every_partition(tmp_path: Path) -> None:
    path = create_hard_disc(tmp_path, filesystem="PFS3", capacity="16MB", partitions=2)
    properties = read_image_properties(path)
    assert properties.image_kind == "hard-disc-image"
    assert properties.layout == "rdb"
    assert properties.layout_label == "Rigid Disk Block partition table"
    assert (properties.heads, properties.sectors_per_track) == (16, 63)
    assert properties.cylinders == properties.capacity_bytes // (16 * 63 * 512)
    assert (properties.disc_vendor, properties.disc_product) == ("AMIGA", "AMIGAFS HDF")
    first, second = properties.volumes
    assert (first.name, second.name) == ("DH0", "DH1")
    assert (first.title, second.title) == ("System", "System1")
    assert first.format == "PFS3" and first.dos_type == "PFS\\3"
    assert first.bootable and not second.bootable
    assert first.first_cylinder == 1
    assert first.last_cylinder is not None and second.first_cylinder == first.last_cylinder + 1


def test_hardfile_and_compressed_image_properties(tmp_path: Path) -> None:
    hardfile = read_image_properties(create_hardfile(tmp_path))
    assert hardfile.layout_label == "One volume, no partition table"
    assert hardfile.cylinders is None
    packed = read_image_properties(gzip_image(create_floppy(tmp_path), tmp_path / "disk.adz"))
    assert packed.image_type == "Compressed Amiga image"
    assert packed.container == "gzip (ADZ/HDZ)"
    assert packed.capacity_bytes == 901_120
    assert packed.cylinders == 80


def test_properties_report_validation_problems_without_refusing(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    invalidate_bitmap(path)
    properties = read_image_properties(path)
    assert properties.validation_state == "problems"
    assert properties.fatal_findings >= 1
    assert properties.first_finding.startswith("bitmap.inconsistent: ")
    assert properties.volumes[0].title == "Workbench"


def test_properties_are_json_serialisable(tmp_path: Path) -> None:
    path = create_hard_disc(tmp_path, capacity="4MB")
    payload = json.loads(json.dumps(read_image_properties(path).as_dict()))
    assert payload["volumes"][1]["name"] == "DH1"
    assert payload["image_path"] == str(path)


def test_known_labels_are_stable_and_image_text_is_preserved() -> None:
    assert kind_label("physical-disc") == "Physical Amiga disc"
    assert kind_label("something-new") == "something-new"
    assert dos_type_text(b"DOS\x00") == "DOS\\0"
    assert dos_type_text(b"PFS\x03") == "PFS\\3"
    assert dos_type_text(b"CD01") == "CD01"
    assert dos_type_text(b"") == ""
