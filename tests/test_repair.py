from __future__ import annotations

import json
from pathlib import Path

import pytest

from amigafs._vendor.amiganut.file import AmigaMeta
from amigafs.core.image import ROOT_INODE, AmigaImage
from amigafs.core.repair import (
    RepairRisk,
    apply_repairs,
    audit_root,
    plan_repairs,
)
from amigafs.errors import AmigaFSError
from amigafs.recovery import pending_recovery
from tests.image_fixture import (
    corrupt_file_header,
    create_floppy,
    create_hard_disc,
    gzip_image,
    invalidate_bitmap,
    scribble_bitmap,
)


def _stale_directory_cache(path: Path) -> None:
    """Change a file header behind the directory cache's back."""

    with AmigaImage.open(path) as image:
        block = image.mount_for(0).stat("Docs/ReadMe").block
    from amigafs._vendor.amiganut.filesystem.blocks import apply_checksum

    data = bytearray(path.read_bytes())
    header = bytearray(data[block * 512 : (block + 1) * 512])
    header[512 - 192 : 512 - 188] = (0x0F).to_bytes(4, "big")
    data[block * 512 : (block + 1) * 512] = apply_checksum(header)
    path.write_bytes(data)


def test_clean_image_needs_no_repair(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    plan = plan_repairs(path)
    assert plan.clean
    assert plan.actions == ()
    assert not plan.application_supported
    assert "No repair actions are needed" in plan.format_text()
    with pytest.raises(AmigaFSError, match="nothing to repair"):
        apply_repairs(path, confirmation=path.name)
    assert not audit_root().exists()


@pytest.mark.parametrize("filesystem", ["OFS", "FFS", "FFS-INTL", "OFS-DC", "FFS-LNFS"])
def test_bitmap_is_rebuilt_exactly(tmp_path: Path, filesystem: str) -> None:
    path = create_floppy(tmp_path, filesystem=filesystem)
    with AmigaImage.open(path, writable=True) as image:
        for number in range(12):
            image.import_file(
                ROOT_INODE,
                f"Extra{number}".encode(),
                bytes([number]) * (number * 9000),
                AmigaMeta(comment="c" * number),
            )
    good = path.read_bytes()
    invalidate_bitmap(path)
    scribble_bitmap(path)
    plan = plan_repairs(path)
    assert [action.action for action in plan.actions] == ["rebuild_allocation_bitmap"]
    assert plan.actions[0].risk is RepairRisk.LOW
    assert plan.actions[0].volumes == (0,)
    assert plan.application_supported
    result = apply_repairs(path, confirmation=path.name)
    assert path.read_bytes() == good
    assert result.report.findings == ()
    assert pending_recovery(path) is None
    audit = json.loads(Path(result.audit_path).read_text(encoding="utf-8"))
    assert audit["status"] == "completed"
    assert audit["checkpoint_retained"] is False
    assert [item["action"] for item in audit["applied_actions"]] == ["rebuild_allocation_bitmap"]
    assert Path(result.audit_path).stat().st_mode & 0o777 == 0o600


def test_stale_directory_cache_is_rebuilt(tmp_path: Path) -> None:
    path = create_floppy(tmp_path, filesystem="FFS-DC")
    _stale_directory_cache(path)
    plan = plan_repairs(path)
    assert [action.action for action in plan.actions] == ["rebuild_directory_cache"]
    assert plan.application_supported
    apply_repairs(path, confirmation=path.name)
    with AmigaImage.open(path, writable=True) as image:
        assert image.node_at_path("Docs/ReadMe").protection == 0x0F
        assert image.integrity_report().findings == ()


def test_repairs_are_applied_bitmap_first(tmp_path: Path) -> None:
    path = create_floppy(tmp_path, filesystem="OFS-DC")
    _stale_directory_cache(path)
    invalidate_bitmap(path)
    plan = plan_repairs(path)
    assert {action.action for action in plan.actions} == {
        "rebuild_allocation_bitmap",
        "rebuild_directory_cache",
    }
    result = apply_repairs(path, confirmation=path.name)
    audit = json.loads(Path(result.audit_path).read_text(encoding="utf-8"))
    assert [item["action"] for item in audit["applied_actions"]] == [
        "rebuild_allocation_bitmap",
        "rebuild_directory_cache",
    ]
    assert result.report.findings == ()


def test_one_partition_of_a_hard_disc_is_repaired_without_touching_the_others(
    tmp_path: Path,
) -> None:
    path = create_hard_disc(tmp_path, capacity="8MB", partitions=2)
    with AmigaImage.open(path) as image:
        second = image.volumes[1]
        root_block = image.mount_for(1).volume.root_block
    from amigafs._vendor.amiganut.filesystem.blocks import apply_checksum

    good = path.read_bytes()
    data = bytearray(good)
    start = second.offset + root_block * 512
    root = bytearray(data[start : start + 512])
    root[512 - 200 : 512 - 196] = bytes(4)
    data[start : start + 512] = apply_checksum(root)
    path.write_bytes(data)
    plan = plan_repairs(path)
    assert plan.actions[0].volumes == (1,)
    assert plan.actions[0].paths == ("DH1",)
    apply_repairs(path, confirmation=path.name)
    assert path.read_bytes() == good


def test_structural_damage_is_never_repaired_automatically(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    corrupt_file_header(path, "C/List")
    before = path.read_bytes()
    plan = plan_repairs(path)
    assert [action.action for action in plan.actions] == ["restore_unreadable_structure"]
    assert plan.actions[0].requires_manual_decision
    assert not plan.application_supported
    assert "cannot be applied automatically" in plan.format_text()
    with pytest.raises(AmigaFSError, match="not eligible for automatic application"):
        apply_repairs(path, confirmation=path.name)
    assert path.read_bytes() == before
    assert pending_recovery(path) is None
    assert not audit_root().exists()


def test_a_repairable_finding_beside_structural_damage_blocks_the_whole_plan(
    tmp_path: Path,
) -> None:
    path = create_floppy(tmp_path)
    corrupt_file_header(path, "C/List")
    invalidate_bitmap(path)
    plan = plan_repairs(path)
    assert not plan.application_supported
    with pytest.raises(AmigaFSError, match="not eligible"):
        apply_repairs(path, confirmation=path.name)


def test_bitmap_rebuild_refuses_a_tree_it_cannot_trust(tmp_path: Path) -> None:
    from amigafs._vendor.amiganut.errors import DataError
    from amigafs.core.bitmap import reachable_blocks, rebuild_bitmap

    path = create_floppy(tmp_path)
    with AmigaImage.open(path) as image:
        volume = image.mount_for(0).volume
        honest = reachable_blocks(volume)
        assert volume.root_block in honest
        blocks = volume._data_blocks
        volume._data_blocks = lambda header: [*blocks(header), volume.root_block]
        with pytest.raises(DataError, match="claimed twice"):
            reachable_blocks(volume)
        volume._data_blocks = lambda header: [*blocks(header), volume.total_blocks + 5]
        with pytest.raises(DataError, match="outside the volume"):
            reachable_blocks(volume)
        volume._data_blocks = blocks
        with pytest.raises(DataError):
            rebuild_bitmap(image.mount_for(0))


def test_confirmation_must_be_the_exact_image_filename(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    invalidate_bitmap(path)
    before = path.read_bytes()
    for wrong in ("", "yes", path.name.upper(), str(path)):
        with pytest.raises(AmigaFSError, match="must exactly match the image filename"):
            apply_repairs(path, confirmation=wrong)
    assert path.read_bytes() == before
    assert not audit_root().exists()


def test_formats_without_a_repair_operation_are_refused(tmp_path: Path) -> None:
    packed = gzip_image(create_floppy(tmp_path), tmp_path / "disk.adz")
    with pytest.raises(AmigaFSError, match="no supported repair operation"):
        apply_repairs(packed, confirmation=packed.name)


def test_failed_repair_retains_its_checkpoint_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = create_floppy(tmp_path)
    invalidate_bitmap(path)
    damaged = path.read_bytes()

    def broken(_mount: object) -> int:
        raise RuntimeError("repair went wrong")

    monkeypatch.setattr("amigafs.core.bitmap.rebuild_bitmap", broken)
    with pytest.raises(AmigaFSError, match="repair went wrong") as failure:
        apply_repairs(path, confirmation=path.name)
    assert "Audit:" in str(failure.value)
    assert path.read_bytes() == damaged
    (audit_path,) = audit_root().glob("*.json")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["status"] == "failed"
    assert audit["checkpoint_created"] is True


def test_plan_json_and_progress_are_stable(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    invalidate_bitmap(path)
    payload = json.loads(json.dumps(plan_repairs(path).as_dict()))
    assert payload["mode"] == "dry-run"
    assert payload["application_supported"] is True
    assert payload["actions"][0]["finding_codes"] == ["bitmap.inconsistent"]
    updates: list[tuple[int, str]] = []
    apply_repairs(
        path, confirmation=path.name, progress=lambda percent, text: updates.append((percent, text))
    )
    percents = [percent for percent, _text in updates]
    assert percents == sorted(percents)
    assert percents[0] == 0 and percents[-1] == 100
