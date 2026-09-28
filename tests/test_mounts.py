from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from amigafs.mounts import (
    MountRecord,
    active_mounts,
    mount_for_image,
    mount_for_image_path,
    mount_source_name,
    parse_mountinfo,
    register_mount,
    registered_mount_at,
    runtime_root,
    unregister_mount,
    wait_for_mount_shutdown,
)
from tests.image_fixture import create_floppy, create_hard_disc


def test_parses_only_amigafs_mounts_and_unescapes_paths() -> None:
    text = "\n".join(
        [
            "31 20 0:29 / /tmp/Amiga\\040Discs ro,nosuid - fuse.amigafs workbench.adf ro",
            "32 20 8:1 / /home rw,relatime - ext4 /dev/sda1 rw",
            "33 20 0:30 / /tmp/acorn ro - fuse.acornfs scsi0.dat ro",
            "malformed line",
        ]
    )
    mounts = parse_mountinfo(text)
    assert len(mounts) == 1
    assert mounts[0].mountpoint == "/tmp/Amiga Discs"
    assert mounts[0].source == "workbench.adf"
    assert mounts[0].options == "ro,nosuid"


def test_mount_source_names_are_safe_for_the_mount_table() -> None:
    assert mount_source_name("workbench.adf") == "workbench.adf"
    assert mount_source_name("Floppy drive A") == "Floppy_drive_A"
    assert mount_source_name("a,b=c d\\e") == "a_b_c_d_e"
    assert mount_source_name("///") == "amiga"
    assert len(mount_source_name("x" * 200)) == 64


def test_registry_enriches_only_kernel_confirmed_mounts(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    registered = register_mount(image_path, mountpoint, read_write=True)
    registry_files = list((runtime_root() / "mounts").glob("*.json"))
    assert len(registry_files) == 1
    assert registry_files[0].stat().st_mode & 0o777 == 0o600
    assert active_mounts() == []
    kernel = MountRecord(str(mountpoint), image_path.name, "rw,nosuid")

    with patch("amigafs.mounts._kernel_mounts", return_value=[kernel]):
        records = active_mounts()
        resolved = mount_for_image(image_path)

    assert records == [
        MountRecord(
            mountpoint=str(mountpoint),
            source=image_path.name,
            options="rw,nosuid",
            image_path=str(image_path.resolve()),
            image_device=image_path.stat().st_dev,
            image_inode=image_path.stat().st_ino,
            image_kind="floppy-image",
            image_name=image_path.name,
            pid=registered.pid,
            read_write=True,
        )
    ]
    assert resolved == records[0]
    assert registered_mount_at(mountpoint) == registered
    unregister_mount(mountpoint)
    assert registered_mount_at(mountpoint) is None


def test_menu_mount_lookup_does_not_resolve_image_content(tmp_path: Path) -> None:
    image_path = create_hard_disc(tmp_path, capacity="4MB")
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    register_mount(image_path, mountpoint, read_write=False)
    kernel = MountRecord(str(mountpoint), image_path.name, "ro")

    with (
        patch("amigafs.mounts._kernel_mounts", return_value=[kernel]),
        patch("amigafs.mounts.resolve_image", side_effect=AssertionError("content inspected")),
    ):
        record = mount_for_image_path(image_path)
        other = mount_for_image_path(create_floppy(tmp_path))

    assert record is not None
    assert record.mountpoint == str(mountpoint)
    assert record.image_kind == "hard-disc-image"
    assert other is None


def test_replaced_image_does_not_match_active_image_identity(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    registered = register_mount(image_path, mountpoint, read_write=False)
    kernel = MountRecord(str(mountpoint), image_path.name, "ro")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(image_path.read_bytes())
    replacement.replace(image_path)

    with patch("amigafs.mounts._kernel_mounts", return_value=[kernel]):
        assert mount_for_image(image_path) is None
        assert mount_for_image_path(image_path) is None
        assert active_mounts()[0].image_inode == registered.image_inode


def test_a_floppy_drive_is_tracked_by_its_private_token(tmp_path: Path) -> None:
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    registered = register_mount("floppy:A", mountpoint, read_write=True)
    assert registered.source == "Floppy_drive_A"
    assert registered.image_kind == "physical-floppy"
    assert registered.image_name == "Floppy drive A"
    assert registered.image_path is not None
    assert Path(registered.image_path).name == "drive-A"
    kernel = MountRecord(str(mountpoint), "Floppy_drive_A", "rw")
    with patch("amigafs.mounts._kernel_mounts", return_value=[kernel]):
        assert mount_for_image("floppy:a") is not None
        assert mount_for_image("floppy:B") is None


def test_a_record_for_a_different_source_name_is_not_trusted(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    register_mount(image_path, mountpoint, read_write=True)
    kernel = MountRecord(str(mountpoint), "something-else.adf", "rw")
    with patch("amigafs.mounts._kernel_mounts", return_value=[kernel]):
        (record,) = active_mounts()
    assert record.image_path is None
    assert record.read_write is None


def test_wait_for_shutdown_covers_post_detach_finalisation() -> None:
    record = MountRecord("/mount", "disc.adf", "rw", pid=123, read_write=True)
    with (
        patch("amigafs.mounts.registered_mount_at", side_effect=[record, None, None]),
        patch("amigafs.mounts.time.monotonic", side_effect=[0.0, 0.1]),
        patch("amigafs.mounts.time.sleep") as sleep,
    ):
        assert wait_for_mount_shutdown("/mount")
    sleep.assert_called_once_with(0.05)


def test_dead_registration_is_pruned(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    register_mount(image_path, mountpoint, read_write=False)
    with (
        patch("amigafs.mounts._kernel_mounts", return_value=[]),
        patch("amigafs.mounts._process_alive", return_value=False),
    ):
        assert active_mounts() == []
    assert list((runtime_root() / "mounts").glob("*.json")) == []
