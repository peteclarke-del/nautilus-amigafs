from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from amigafs.core import disc_transfer
from amigafs.core.device_policy import PhysicalDisc
from amigafs.core.disc_transfer import read_disc, write_disc
from amigafs.core.image import AmigaImage
from amigafs.errors import AmigaFSError, DeviceAccessError, OperationCancelled
from tests.image_fixture import create_floppy, create_hard_disc


class PretendDisc:
    """An ordinary file standing in for a block device."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, backing: Path) -> None:
        self.backing = backing
        self.opened: list[dict[str, Any]] = []
        self.disc = PhysicalDisc(
            name="sdz",
            device="/dev/sdz",
            stable_path="/dev/disk/by-id/usb-Pretend_Card",
            model="Pretend Card",
            size=backing.stat().st_size,
            removable=True,
            usb=True,
            read_only=False,
        )
        monkeypatch.setattr(disc_transfer, "describe_disc", lambda _device: self.disc)
        monkeypatch.setattr(disc_transfer, "open_device", self.open)

    def open(self, device: str, *, writable: bool, allow_blank: bool = False) -> int:
        self.opened.append({"device": device, "writable": writable, "allow_blank": allow_blank})
        return os.open(self.backing, os.O_RDWR if writable else os.O_RDONLY)


def test_read_disc_copies_every_byte_without_touching_the_disc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backing = create_hard_disc(tmp_path, capacity="4MB")
    original = backing.read_bytes()
    disc = PretendDisc(monkeypatch, backing)
    out = tmp_path / "out"
    out.mkdir()
    updates: list[int] = []
    result = read_disc(
        "/dev/sdz", out / "card.hdf", progress=lambda percent, _text: updates.append(percent)
    )
    assert result.image == out / "card.hdf"
    assert result.size == len(original)
    assert result.sha256 == hashlib.sha256(original).hexdigest()
    assert result.image.read_bytes() == original
    assert backing.read_bytes() == original
    assert disc.opened == [{"device": "/dev/sdz", "writable": False, "allow_blank": False}]
    assert updates == sorted(updates) and updates[-1] == 100
    # Runs of empty sectors are stored as holes, not as megabytes of zeros.
    assert result.image.stat().st_blocks * 512 < len(original)
    assert sorted(child.name for child in out.iterdir()) == ["card.hdf"]
    with AmigaImage.open(result.image) as image:
        assert image.integrity_report().findings == ()


def test_read_disc_never_overwrites_and_cleans_up_when_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backing = create_hard_disc(tmp_path, capacity="4MB")
    PretendDisc(monkeypatch, backing)
    out = tmp_path / "out"
    out.mkdir()
    (out / "CARD.HDF").write_bytes(b"precious")
    with pytest.raises(AmigaFSError, match="Refusing to overwrite"):
        read_disc("/dev/sdz", out / "card.hdf")
    assert (out / "CARD.HDF").read_bytes() == b"precious"
    with pytest.raises(OperationCancelled):
        read_disc("/dev/sdz", out / "other.hdf", cancelled=lambda: True)
    with pytest.raises(AmigaFSError, match="does not exist"):
        read_disc("/dev/sdz", tmp_path / "nowhere" / "card.hdf")
    assert sorted(child.name for child in out.iterdir()) == ["CARD.HDF"]


def test_read_disc_needs_room_for_the_whole_disc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    PretendDisc(monkeypatch, create_hard_disc(tmp_path, capacity="4MB"))
    monkeypatch.setattr(
        "amigafs.core.disc_transfer.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": 1024})(),
    )
    with pytest.raises(AmigaFSError, match="less than the"):
        read_disc("/dev/sdz", tmp_path / "card.hdf")
    assert not (tmp_path / "card.hdf").exists()


def test_write_disc_replaces_the_disc_and_verifies_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = create_hard_disc(tmp_path, capacity="4MB")
    backing = tmp_path / "card.bin"
    backing.write_bytes(b"\xee" * (image.stat().st_size + 64 * 512))
    disc = PretendDisc(monkeypatch, backing)
    result = write_disc(image, "/dev/sdz", confirmation="sdz")
    written = backing.read_bytes()
    assert written[: image.stat().st_size] == image.read_bytes()
    # Space beyond the image is left alone.
    assert written[image.stat().st_size :] == b"\xee" * (64 * 512)
    assert result.sha256 == hashlib.sha256(image.read_bytes()).hexdigest()
    assert disc.opened == [{"device": "/dev/sdz", "writable": True, "allow_blank": True}]


@pytest.mark.parametrize("confirmation", ["", "yes", "SDZ", "/dev/sdz", "sdz "])
def test_write_disc_demands_the_exact_device_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirmation: str
) -> None:
    image = create_hard_disc(tmp_path, capacity="4MB")
    backing = tmp_path / "card.bin"
    backing.write_bytes(bytes(image.stat().st_size))
    disc = PretendDisc(monkeypatch, backing)
    with pytest.raises(AmigaFSError, match="must exactly match the device name: sdz"):
        write_disc(image, "/dev/sdz", confirmation=confirmation)
    assert disc.opened == []
    assert backing.read_bytes() == bytes(image.stat().st_size)


def test_write_disc_refuses_unsuitable_images_and_discs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = create_hard_disc(tmp_path, capacity="4MB")
    small = tmp_path / "small.bin"
    small.write_bytes(bytes(1024 * 1024))
    disc = PretendDisc(monkeypatch, small)
    with pytest.raises(AmigaFSError, match="holds only"):
        write_disc(image, "/dev/sdz", confirmation="sdz")
    assert small.read_bytes() == bytes(1024 * 1024)
    disc.opened.clear()
    with pytest.raises(AmigaFSError, match="Only a plain Amiga hard-disc image"):
        write_disc(create_floppy(tmp_path), "/dev/sdz", confirmation="sdz")
    assert disc.opened == []


def test_write_disc_refuses_a_mounted_image_or_an_interrupted_disc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amigafs.mounts import MountRecord
    from amigafs.recovery import RecoveryInfo

    image = create_hard_disc(tmp_path, capacity="4MB")
    backing = tmp_path / "card.bin"
    backing.write_bytes(bytes(image.stat().st_size))
    disc = PretendDisc(monkeypatch, backing)
    monkeypatch.setattr(
        "amigafs.mounts.mount_for_image_path",
        lambda _path: MountRecord("/mnt", "harddisk.hdf", "rw"),
    )
    with pytest.raises(AmigaFSError, match="Unmount the image"):
        write_disc(image, "/dev/sdz", confirmation="sdz")
    monkeypatch.setattr("amigafs.mounts.mount_for_image_path", lambda _path: None)
    pending = RecoveryInfo(1, "x", "/dev/sdz", "journal", "now", "ready", 1, True)
    monkeypatch.setattr("amigafs.recovery.pending_recovery", lambda _path: pending)
    with pytest.raises(AmigaFSError, match="interrupted writable session"):
        write_disc(image, "/dev/sdz", confirmation="sdz")
    assert disc.opened == []


def test_write_disc_detects_a_disc_that_does_not_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = create_hard_disc(tmp_path, capacity="4MB")
    backing = tmp_path / "card.bin"
    backing.write_bytes(bytes(image.stat().st_size))
    PretendDisc(monkeypatch, backing)
    real_pread = os.pread

    def failing_pread(descriptor: int, length: int, offset: int) -> bytes:
        data = real_pread(descriptor, length, offset)
        return b"\xff" + data[1:] if offset == 0 else data

    monkeypatch.setattr("amigafs.core.disc_transfer.os.pread", failing_pread)
    with pytest.raises(AmigaFSError, match="does not read back as it was written"):
        write_disc(image, "/dev/sdz", confirmation="sdz")


def test_refused_devices_are_reported_before_anything_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_device: object) -> PhysicalDisc:
        raise DeviceAccessError("This is one of the computer's own discs.")

    monkeypatch.setattr(disc_transfer, "describe_disc", refuse)
    with pytest.raises(DeviceAccessError, match="own discs"):
        read_disc("/dev/sda", tmp_path / "system.hdf")
    with pytest.raises(DeviceAccessError, match="own discs"):
        write_disc(create_hard_disc(tmp_path, capacity="4MB"), "/dev/sda", confirmation="sda")
    assert not (tmp_path / "system.hdf").exists()
