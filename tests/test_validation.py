from __future__ import annotations

import json
from pathlib import Path

import pytest

from amigafs.core.image import AmigaImage
from amigafs.core.validation import (
    VALIDATION_REPORT_SCHEMA_VERSION,
    FindingSeverity,
    classify_problem,
    require_safe_for_write,
    validate_image_report,
)
from amigafs.errors import AmigaFSError, OperationCancelled, OperationLimitExceeded
from amigafs.operations import OperationBudget
from amigafs.recovery import pending_recovery
from tests.image_fixture import (
    corrupt_file_header,
    create_floppy,
    create_hard_disc,
    invalidate_bitmap,
    scribble_bitmap,
)


@pytest.mark.parametrize("filesystem", ["OFS", "FFS-DC", "FFS-LNFS"])
def test_clean_floppy_has_complete_capacity_accounting(tmp_path: Path, filesystem: str) -> None:
    report = validate_image_report(create_floppy(tmp_path, filesystem=filesystem))
    assert report.findings == ()
    assert report.safe_for_write
    assert report.layout == "single"
    (volume,) = report.volumes
    assert volume.title == "Workbench"
    assert volume.format == filesystem
    assert volume.entries == 10
    assert volume.used_bytes is not None and volume.free_bytes is not None
    assert volume.used_bytes + volume.free_bytes == volume.total_bytes
    assert report.format_text() == "Filesystem validation passed with no problems."


@pytest.mark.parametrize("filesystem", ["FFS-INTL", "PFS3", "SFS"])
def test_clean_hard_disc_reports_every_partition(tmp_path: Path, filesystem: str) -> None:
    path = create_hard_disc(tmp_path, filesystem=filesystem, capacity="12MB", partitions=3)
    report = validate_image_report(path)
    assert report.findings == ()
    assert report.layout == "rdb"
    assert [volume.name for volume in report.volumes] == ["DH0", "DH1", "DH2"]
    assert all(volume.mounted for volume in report.volumes)


def test_validation_json_has_a_versioned_compatibility_contract(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    invalidate_bitmap(path)
    payload = json.loads(json.dumps(validate_image_report(path).as_dict()))
    assert payload["schema_version"] == VALIDATION_REPORT_SCHEMA_VERSION == 1
    assert payload["compatibility_profile"] == {"id": "amigafs-floppy-image", "version": 1}
    assert payload["safe_for_write"] is False
    assert payload["summary"]["fatal"] == len(payload["findings"]) >= 1
    assert set(payload["findings"][0]) == {"severity", "code", "message", "path", "volume"}
    assert payload["volumes"][0]["name"] == ""
    assert payload["image_kind"] == "floppy-image"


def test_invalid_and_inconsistent_bitmaps_are_classified_as_repairable(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    invalidate_bitmap(path)
    scribble_bitmap(path)
    report = validate_image_report(path)
    assert {finding.code for finding in report.findings} == {"bitmap.inconsistent"}
    assert all(finding.severity is FindingSeverity.FATAL for finding in report.findings)
    with pytest.raises(AmigaFSError, match="Writable mount refused"):
        require_safe_for_write(report)


def test_damaged_file_header_is_a_structural_finding(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    corrupt_file_header(path, "Docs/ReadMe")
    report = validate_image_report(path)
    assert "volume.structure" in {finding.code for finding in report.findings}
    assert any("Docs/ReadMe" in finding.message for finding in report.findings)


def test_classification_depends_on_the_filesystem() -> None:
    message = "The block-allocation bitmap is marked invalid."
    assert classify_problem(message, "ffs") == "bitmap.inconsistent"
    assert classify_problem(message, "ofs") == "bitmap.inconsistent"
    assert classify_problem(message, "pfs3") == "volume.structure"
    assert (
        classify_problem("The cache of Docs has no record for ReadMe.", "ffs") == "dircache.stale"
    )
    assert classify_problem("No directory cache was found for the root.", "ofs") == "dircache.stale"
    assert classify_problem("Docs/ReadMe has a bad header checksum.", "ffs") == "volume.structure"


def test_writable_gate_refuses_damage_without_creating_a_checkpoint(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    scribble_bitmap(path)
    before = path.read_bytes()
    with pytest.raises(AmigaFSError, match="Writable mount refused"):
        AmigaImage.open(path, writable=True)
    assert pending_recovery(path) is None
    assert path.read_bytes() == before
    with AmigaImage.open(path) as image:
        assert image.read(image.node_at_path("Docs/ReadMe").inode, 0, 4) == b"Read"


def test_an_unreadable_image_is_a_classified_fatal_finding(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    data = bytearray(path.read_bytes())
    data[880 * 512 : 881 * 512] = bytes(512)
    path.write_bytes(data)
    report = validate_image_report(path)
    assert [finding.code for finding in report.findings] == ["image.open_failed"]
    assert not report.safe_for_write


def test_unsupported_partitions_are_advice_and_the_rest_still_mounts(tmp_path: Path) -> None:
    path = create_hard_disc(tmp_path, capacity="8MB", partitions=2)
    data = bytearray(path.read_bytes())
    # The second PART block: give it a DOS type AmigaFS has no driver for.
    second = 2 * 512
    assert data[second : second + 4] == b"PART"
    data[second + 128 + 64 : second + 128 + 68] = b"CD01"
    checksum_base = bytearray(data[second : second + 512])
    checksum_base[8:12] = bytes(4)
    total = sum(
        int.from_bytes(checksum_base[index : index + 4], "big") for index in range(0, 256, 4)
    )
    data[second + 8 : second + 12] = ((-total) & 0xFFFFFFFF).to_bytes(4, "big")
    path.write_bytes(data)
    report = validate_image_report(path)
    assert [(finding.code, finding.severity) for finding in report.findings] == [
        ("volume.not_mounted", FindingSeverity.ADVICE)
    ]
    assert report.safe_for_write
    with AmigaImage.open(path) as image:
        assert image.mounted_volumes == (0,)


def test_validation_text_is_complete_and_stable(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    invalidate_bitmap(path)
    text = validate_image_report(path).format_text()
    assert text.startswith("Validation found ")
    assert "[FATAL] bitmap.inconsistent" in text


def test_validation_can_be_cancelled_and_is_bounded(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with pytest.raises(OperationCancelled):
        validate_image_report(path, cancelled=lambda: True)
    with pytest.raises(OperationLimitExceeded):
        validate_image_report(path, budget=OperationBudget.create(max_items=2))
    assert pending_recovery(path) is None


def test_fragmented_and_nearly_full_images_remain_valid(tmp_path: Path) -> None:
    from amigafs._vendor.amiganut.file import AmigaMeta
    from amigafs.core.image import ROOT_INODE

    path = create_floppy(tmp_path, files=())
    with AmigaImage.open(path, writable=True, commit_validation_bytes=0) as image:
        for number in range(40):
            image.import_file(
                ROOT_INODE, f"F{number:02d}".encode(), bytes([number]) * 9000, AmigaMeta()
            )
        for number in range(0, 40, 2):
            image.remove(ROOT_INODE, f"F{number:02d}".encode(), directory=False)
        image.import_file(ROOT_INODE, b"Spread", bytes(200_000), AmigaMeta())
    report = validate_image_report(path)
    assert report.findings == ()
