from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from amigafs.core.image import ROOT_INODE, AmigaImage
from amigafs.errors import AmigaFSError, OperationCancelled
from amigafs.recovery import (
    JOURNAL_NAME,
    canonical_source,
    checkpoint_directory,
    pending_physical_recoveries,
    pending_recovery,
    recover_image,
    salvage_workspace,
    state_root,
)
from tests.image_fixture import create_floppy, create_hard_disc


def _interrupted(path: Path) -> bytes:
    """Change an image, then abandon the session as a crash would."""

    original = path.read_bytes()
    image = AmigaImage.open(path, writable=True)
    top = image.children[ROOT_INODE][0] if image.layout == "rdb" else ROOT_INODE
    image.make_directory(top, b"Interrupted")
    node = image.create_file(top, b"Partial")
    image.replace_file(node.inode, b"x" * 50_000)
    image.store.handle.close()
    assert path.read_bytes() != original
    return original


def test_recovery_module_imports_cleanly_in_fresh_process() -> None:
    for module in (
        "amigafs.recovery",
        "amigafs.mounts",
        "amigafs.core.media",
        "amigafs.core.blockio",
        "amigafs.core",
        "amigafs.desktop",
        "amigafs.cli",
    ):
        subprocess.run([sys.executable, "-c", f"import {module}"], check=True)


def test_clean_writable_session_removes_its_checkpoint(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    with AmigaImage.open(path, writable=True) as image:
        directory = checkpoint_directory(path)
        assert (directory / "manifest.json").is_file()
        assert (directory / JOURNAL_NAME).is_file()
        assert directory.stat().st_mode & 0o777 == 0o700
        image.create_file(ROOT_INODE, b"Kept")
    assert pending_recovery(path) is None
    assert not directory.exists()
    assert not state_root().exists() or not any(state_root().iterdir())


def test_journal_costs_what_changed_not_what_the_image_holds(tmp_path: Path) -> None:
    path = create_hard_disc(tmp_path, filesystem="PFS3", capacity="32MB", partitions=1)
    with AmigaImage.open(path, writable=True) as image:
        image.make_directory(image.children[ROOT_INODE][0], b"Small")
        journal = checkpoint_directory(path) / JOURNAL_NAME
        assert 0 < journal.stat().st_size < 256 * 1024
        assert path.stat().st_size > 30 * 1024 * 1024


def test_checkpoint_creation_replaces_an_orphan_journal(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    directory = checkpoint_directory(path)
    directory.mkdir(parents=True, mode=0o700)
    (directory / JOURNAL_NAME).write_bytes(b"left by a crash after the manifest was removed")
    with AmigaImage.open(path, writable=True) as image:
        image.create_file(ROOT_INODE, b"Fine")
    assert pending_recovery(path) is None


@pytest.mark.parametrize("filesystem", ["FFS", "PFS3", "SFS"])
def test_interrupted_session_can_restore_the_pre_mount_image(
    tmp_path: Path, filesystem: str
) -> None:
    if filesystem == "FFS":
        path = create_floppy(tmp_path)
    else:
        path = create_hard_disc(tmp_path, filesystem=filesystem, capacity="8MB", partitions=1)
    original = _interrupted(path)
    info = pending_recovery(path)
    assert info is not None
    assert (info.kind, info.state, info.size, info.is_device) == (
        "journal",
        "ready",
        len(original),
        False,
    )
    with pytest.raises(AmigaFSError, match="needs recovery"):
        AmigaImage.open(path, writable=True)
    # The interrupted image can still be inspected read-only.
    with AmigaImage.open(path):
        pass
    assert "--restore" in recover_image(path)
    assert recover_image(path, restore=True) == "Recovery checkpoint restored."
    assert path.read_bytes() == original
    assert pending_recovery(path) is None
    with AmigaImage.open(path, writable=True) as image:
        assert image.integrity_report().findings == ()


def test_interrupted_session_can_be_accepted_as_it_stands(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    _interrupted(path)
    current = path.read_bytes()
    assert recover_image(path, discard=True) == "Recovery checkpoint discarded."
    assert path.read_bytes() == current
    assert pending_recovery(path) is None
    with AmigaImage.open(path) as image:
        assert image.node_at_path("Partial").size == 50_000
        assert image.integrity_report().findings == ()


def test_crashed_writer_leaves_a_restorable_checkpoint(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    original = path.read_bytes()
    script = f"""
import os
from amigafs.core.image import ROOT_INODE, AmigaImage
image = AmigaImage.open({str(path)!r}, writable=True)
node = image.create_file(ROOT_INODE, b"Doomed")
image.replace_file(node.inode, b"y" * 20000)
os._exit(7)
"""
    result = subprocess.run([sys.executable, "-c", script], check=False, env=os.environ.copy())
    assert result.returncode == 7
    assert path.read_bytes() != original
    assert pending_recovery(path) is not None
    recover_image(path, restore=True)
    assert path.read_bytes() == original


def test_restore_refuses_an_image_the_checkpoint_does_not_belong_to(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    _interrupted(path)
    interrupted = path.read_bytes()
    # Another image of the same size now sits at the same path.
    # It differs where the session wrote nothing, so the journal cannot make it right.
    other = create_floppy(tmp_path, name="other", title="Different", bootable=True)
    path.write_bytes(other.read_bytes())
    with pytest.raises(AmigaFSError, match="does not match the recovery checkpoint"):
        recover_image(path, restore=True)
    assert path.read_bytes() == other.read_bytes()
    assert pending_recovery(path) is not None
    path.write_bytes(interrupted[:-512])
    with pytest.raises(AmigaFSError, match="not the image this checkpoint belongs to"):
        recover_image(path, restore=True)


def test_restore_refuses_a_mounted_image(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    _interrupted(path)
    with AmigaImage.open(path), pytest.raises(AmigaFSError, match="another AmigaFS process"):
        recover_image(path, restore=True)
    assert pending_recovery(path) is not None


def test_cancelled_restore_changes_nothing(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    _interrupted(path)
    current = path.read_bytes()
    with pytest.raises(OperationCancelled):
        recover_image(path, restore=True, cancelled=lambda: True)
    assert path.read_bytes() == current
    assert pending_recovery(path) is not None


def test_restore_and_discard_are_mutually_exclusive(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    assert recover_image(path) == "No recovery checkpoint is pending."
    _interrupted(path)
    with pytest.raises(AmigaFSError, match="not both"):
        recover_image(path, restore=True, discard=True)


def test_unreadable_manifest_is_reported_not_ignored(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    _interrupted(path)
    manifest = checkpoint_directory(path) / "manifest.json"
    manifest.write_text("{not json", encoding="utf-8")
    with pytest.raises(AmigaFSError, match="recovery manifest is unreadable"):
        pending_recovery(path)
    manifest.write_text(json.dumps({"version": 99}), encoding="utf-8")
    with pytest.raises(AmigaFSError, match="recovery manifest is unreadable"):
        pending_recovery(path)


def test_salvage_applies_only_to_working_copies(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    _interrupted(path)
    with pytest.raises(AmigaFSError, match="No interrupted working copy"):
        salvage_workspace(path, tmp_path / "out.adf")


def test_stable_device_names_are_recorded_unresolved(tmp_path: Path) -> None:
    assert canonical_source("/dev/disk/by-id/usb-Example_123") == Path(
        "/dev/disk/by-id/usb-Example_123"
    )
    link = tmp_path / "link.adf"
    target = create_floppy(tmp_path)
    link.symlink_to(target)
    assert canonical_source(link) == target.resolve()


def test_only_sources_without_a_file_are_listed_for_the_background_menu(tmp_path: Path) -> None:
    path = create_floppy(tmp_path)
    _interrupted(path)
    assert pending_physical_recoveries() == ()
    manifest = checkpoint_directory(path) / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["is_device"] = True
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    (listed,) = pending_physical_recoveries()
    assert listed.image_path == str(path)
