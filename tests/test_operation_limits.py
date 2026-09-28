from pathlib import Path

import pytest

from amigafs.core.image import ROOT_INODE, AmigaImage
from amigafs.core.properties import read_image_properties
from amigafs.core.repair import apply_repairs, plan_repairs
from amigafs.core.validation import validate_image_report
from amigafs.errors import OperationLimitExceeded
from amigafs.operations import OperationBudget
from tests.image_fixture import create_floppy, create_hard_disc, invalidate_bitmap


def _expired_budget() -> OperationBudget:
    return OperationBudget(deadline=0.0, clock=lambda: 1.0)


@pytest.mark.parametrize("operation", ["validation", "properties", "repair-plan"])
def test_inspection_operations_stop_at_their_wall_clock_budget(
    tmp_path: Path, operation: str
) -> None:
    image_path = create_floppy(tmp_path)

    with pytest.raises(OperationLimitExceeded, match="safe time limit"):
        if operation == "validation":
            validate_image_report(image_path, budget=_expired_budget())
        elif operation == "properties":
            read_image_properties(image_path, budget=_expired_budget())
        else:
            plan_repairs(image_path, budget=_expired_budget())


def test_validation_stops_at_total_item_limit(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    budget = OperationBudget.create(timeout=30, max_items=1)

    with pytest.raises(OperationLimitExceeded, match="safe item limit"):
        validate_image_report(image_path, budget=budget)


def test_validation_stops_at_directory_depth_limit(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    validate_image_report(image_path, budget=OperationBudget.create(timeout=30, max_depth=3))

    with pytest.raises(OperationLimitExceeded, match="directory-depth limit"):
        validate_image_report(image_path, budget=OperationBudget.create(timeout=30, max_depth=2))


def test_partition_folders_do_not_count_towards_directory_depth(tmp_path: Path) -> None:
    image_path = create_hard_disc(tmp_path, capacity="4MB", partitions=1, files=())
    with AmigaImage.open(image_path, writable=True) as image:
        image.make_directory(image.children[ROOT_INODE][0], b"One")
    validate_image_report(image_path, budget=OperationBudget.create(timeout=30, max_depth=1))


def test_properties_preserve_operation_limit_error(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    times = iter((0.0, 0.0, 31.0, 31.0, 31.0))
    budget = OperationBudget.create(timeout=30, clock=lambda: next(times))

    with pytest.raises(OperationLimitExceeded, match="safe time limit"):
        read_image_properties(image_path, budget=budget)


def test_repair_timeout_precedes_audit_checkpoint_and_mutation(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    invalidate_bitmap(image_path)
    before = image_path.read_bytes()

    with pytest.raises(OperationLimitExceeded, match="safe time limit"):
        apply_repairs(image_path, confirmation=image_path.name, budget=_expired_budget())

    assert image_path.read_bytes() == before
    assert not [path for path in (tmp_path / "state").rglob("*") if path.is_file()]
