from __future__ import annotations

import json
import os
import shlex
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from amigafs.core import device_policy, devices
from amigafs.core.device_policy import (
    DevicePolicyError,
    amiga_evidence,
    list_physical_discs,
    resolve_disc,
)
from amigafs.errors import DeviceAccessError

POLICY_SOURCE = Path(device_policy.__file__)


class Host:
    """A pretend /sys, /dev and /proc describing the discs of one machine."""

    def __init__(self, root: Path) -> None:
        self.sys_block = root / "sys" / "block"
        self.by_id = root / "dev" / "disk" / "by-id"
        self.proc_mounts = root / "proc" / "mounts"
        self.proc_swaps = root / "proc" / "swaps"
        self.udev_data = root / "run" / "udev" / "data"
        for directory in (self.sys_block, self.by_id, self.udev_data, self.proc_mounts.parent):
            directory.mkdir(parents=True, exist_ok=True)
        self.proc_mounts.write_text("", encoding="utf-8")
        self.proc_swaps.write_text("Filename Type Size Used Priority\n", encoding="utf-8")
        self.bus = root / "devices"

    def add(
        self,
        name: str,
        *,
        usb: bool = True,
        removable: bool = False,
        sectors: int = 8_000_000,
        read_only: bool = False,
        model: str = "CompactFlash",
        stable: str | None = None,
    ) -> None:
        bus = "usb1/1-1" if usb else "pci0000:00/ata1"
        target = self.bus / bus / "block" / name
        target.mkdir(parents=True)
        (target / "size").write_text(str(sectors), encoding="utf-8")
        (target / "removable").write_text("1" if removable else "0", encoding="utf-8")
        (target / "ro").write_text("1" if read_only else "0", encoding="utf-8")
        (target / "dev").write_text("8:16", encoding="utf-8")
        (target / "holders").mkdir()
        device = target / "device"
        device.mkdir()
        (device / "model").write_text(model, encoding="utf-8")
        (self.sys_block / name).symlink_to(target)
        if stable is not None:
            (self.by_id / stable).symlink_to(f"/dev/{name}")

    def locations(self) -> dict[str, Path]:
        return {
            "sys_block": self.sys_block,
            "by_id": self.by_id,
            "proc_mounts": self.proc_mounts,
            "proc_swaps": self.proc_swaps,
            "udev_data": self.udev_data,
        }


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path / "host")


def test_only_removable_and_usb_whole_discs_are_offered(host: Host) -> None:
    host.add("sda", usb=False, model="Internal SSD")
    host.add("sdb", usb=True, model="CF Card", stable="usb-Generic_CF_0001")
    host.add("sdc", usb=False, removable=True, model="Hot-swap bay")
    host.add("mmcblk0", usb=False, model="SD Card")
    host.add("sdd", usb=True, sectors=0, model="Empty reader")
    host.add("loop0", usb=True)
    host.add("nvme0n1", usb=True)
    host.add("sdb1", usb=True)
    offered = list_physical_discs(**host.locations())
    assert [disc.name for disc in offered] == ["mmcblk0", "sdb", "sdc"]
    card = offered[1]
    assert card.size == 8_000_000 * 512
    assert card.model == "CF Card"
    assert card.stable_path.endswith("/by-id/usb-Generic_CF_0001")
    assert card.eligible
    refused = list_physical_discs(**host.locations(), include_refused=True)
    # The computer's own disc is not even mentioned.
    assert [disc.name for disc in refused] == ["mmcblk0", "sdb", "sdc", "sdd"]
    assert refused[-1].refusal_code == "no-media"


def test_mounted_swap_and_held_discs_are_refused(host: Host) -> None:
    host.add("sdb")
    host.add("sdc")
    host.add("sdd")
    host.proc_mounts.write_text(
        "/dev/sdb1 /media/user/My\\040Card vfat rw 0 0\n/dev/sda1 / ext4 rw 0 0\n",
        encoding="utf-8",
    )
    host.proc_swaps.write_text(
        "Filename Type Size Used Priority\n/dev/sdc2 partition 1 0 -2\n", encoding="utf-8"
    )
    (host.sys_block / "sdd" / "holders" / "dm-0").mkdir()
    assert list_physical_discs(**host.locations()) == []
    by_name = {
        disc.name: disc for disc in list_physical_discs(**host.locations(), include_refused=True)
    }
    assert by_name["sdb"].refusal_code == "mounted"
    assert by_name["sdb"].mounted == ("/media/user/My Card",)
    assert by_name["sdc"].refusal_code == "in-use"
    assert by_name["sdd"].refusal_code == "in-use"


def test_stable_name_prefers_the_drive_over_its_adapter(host: Host) -> None:
    host.add("sdb")
    for name in ("usb-JMicron_Generic_0123", "ata-SanDisk_SDCFX_9", "wwn-0x5000"):
        (host.by_id / name).symlink_to("/dev/sdb")
    (host.by_id / "ata-SanDisk_SDCFX_9-part1").symlink_to("/dev/sdb1")
    assert device_policy.stable_path("sdb", host.by_id) == str(host.by_id / "ata-SanDisk_SDCFX_9")
    assert device_policy.stable_path("sdz", host.by_id) is None


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"RDSK" + bytes(60), "rdb"),
        (bytes(512 * 7) + b"RDSK" + bytes(60), "rdb"),
        (bytes(512 * 15) + b"RDSK", "rdb"),
        (bytes(512 * 16) + b"RDSK", ""),
        (bytes(100) + b"RDSK", ""),
        (b"DOS\x00" + bytes(60), "volume"),
        (b"DOS\x07", "volume"),
        (b"PFS\x03", "volume"),
        (b"SFS\x00", "volume"),
        (b"DOS\x08", ""),
        (b"DOSX", ""),
        (b"\xeb\x3c\x90MSDOS5.0", ""),
        (b"", ""),
        (bytes(8192), ""),
    ],
)
def test_amiga_evidence(head: bytes, expected: str) -> None:
    assert amiga_evidence(head) == expected


def test_paths_that_are_not_whole_removable_discs_are_refused(host: Host, tmp_path: Path) -> None:
    regular = tmp_path / "image.hdf"
    regular.write_bytes(b"RDSK")
    with pytest.raises(DevicePolicyError) as not_block:
        resolve_disc(regular, **host.locations())
    assert not_block.value.code == "not-block"
    with pytest.raises(DevicePolicyError) as missing:
        resolve_disc(tmp_path / "absent", **host.locations())
    assert missing.value.code == "missing"
    for device in ("/dev/loop0", "/dev/null"):
        if not os.path.exists(device) or not stat.S_ISBLK(os.stat(device).st_mode):
            continue
        with pytest.raises(DevicePolicyError) as virtual:
            resolve_disc(device, **host.locations())
        assert virtual.value.code == "not-whole-disc"


def test_refusals_are_translated_for_the_user(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_selected: object, **_kwargs: object) -> None:
        raise DevicePolicyError("mounted", "internal wording")

    monkeypatch.setattr(device_policy, "resolve_disc", refuse)
    with pytest.raises(DeviceAccessError, match="Unmount it there first"):
        devices.describe_disc("/dev/sdb")
    with pytest.raises(DeviceAccessError, match="Unmount it there first"):
        devices.open_device("/dev/sdb", writable=False)


def test_helper_is_trusted_only_when_root_owns_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = tmp_path / "amigafs-device-helper"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    monkeypatch.setattr(devices, "HELPER_LOCATIONS", (tmp_path / "absent", helper))
    # A helper the user could rewrite must never be run as root.
    assert (devices.trusted_helper() is None) == (os.geteuid() != 0)
    with pytest.raises(DeviceAccessError, match="device helper is not installed"):
        monkeypatch.setattr(devices, "trusted_helper", lambda: None)
        devices.open_through_helper("/dev/sdb", writable=False)


def test_helper_source_is_self_contained_and_isolated() -> None:
    source = POLICY_SOURCE.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/python3 -I\n")
    imported = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}
    assert "amigafs" not in imported


def test_helper_refuses_to_run_without_a_socket(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(POLICY_SOURCE), "--device", "/dev/null"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "must be started by AmigaFS" in result.stderr


def _run_helper(arguments: list[str]) -> tuple[int, dict[str, object], int | None]:
    ours, theirs = socket.socketpair()
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", str(POLICY_SOURCE), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=theirs.fileno(),
            stderr=subprocess.PIPE,
        )
        theirs.close()
        ours.settimeout(20)
        reply, descriptor = devices._receive(ours)
        process.communicate(timeout=20)
        return process.returncode, reply, descriptor
    finally:
        ours.close()


def test_helper_reports_a_refusal_over_its_socket(tmp_path: Path) -> None:
    regular = tmp_path / "image.hdf"
    regular.write_bytes(b"RDSK" + bytes(8192))
    code, reply, descriptor = _run_helper(["--device", str(regular), "--mode", "rw"])
    assert code == 1
    assert descriptor is None
    assert reply["ok"] is False
    assert reply["code"] == "not-block"
    assert reply["protocol"] == device_policy.HELPER_PROTOCOL


def test_client_turns_a_helper_refusal_into_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    regular = tmp_path / "image.hdf"
    regular.write_bytes(b"RDSK" + bytes(8192))
    launcher = tmp_path / "launcher"
    launcher.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I "$@"\n', encoding="utf-8"
    )
    launcher.chmod(0o755)
    with pytest.raises(DeviceAccessError, match="not a physical disc"):
        devices.open_through_helper(
            regular, writable=False, helper=POLICY_SOURCE, launcher=str(launcher)
        )
    denied = tmp_path / "denied"
    denied.write_text("#!/bin/sh\nexit 126\n", encoding="utf-8")
    denied.chmod(0o755)
    with pytest.raises(DeviceAccessError, match="was not granted"):
        devices.open_through_helper(
            regular, writable=False, helper=POLICY_SOURCE, launcher=str(denied)
        )
    monkeypatch.setattr("amigafs.core.devices.shutil.which", lambda _name: None)
    with pytest.raises(DeviceAccessError, match="pkexec"):
        devices.open_through_helper(regular, writable=False, helper=POLICY_SOURCE)


def test_received_descriptor_must_be_the_requested_disc_with_the_requested_access(
    tmp_path: Path,
) -> None:
    regular = tmp_path / "file"
    regular.write_bytes(b"x")
    descriptor = os.open(regular, os.O_RDONLY)
    try:
        with pytest.raises(DeviceAccessError, match="not a disc"):
            devices._verify_received(descriptor, regular, writable=False)
    finally:
        os.close(descriptor)


def test_descriptor_is_handed_to_a_daemon_of_the_same_user(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.write_bytes(b"handed over")
    descriptor = os.open(payload, os.O_RDONLY)
    handover = devices.DescriptorHandover(tmp_path, ".mount.device")
    try:
        assert stat.S_IMODE(handover.path.stat().st_mode) == 0o600
        assert not handover.serve_once(descriptor)
        received: list[tuple[dict[str, object], int | None]] = []

        def daemon() -> None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.connect(str(handover.path))
                received.append(devices._receive(channel))

        thread = threading.Thread(target=daemon)
        thread.start()
        assert handover.serve_once(descriptor, timeout=10)
        thread.join(timeout=10)
        reply, passed = received[0]
        assert reply["ok"] is True
        assert passed is not None and passed != descriptor
        assert os.pread(passed, 20, 0) == b"handed over"
        os.close(passed)
    finally:
        handover.close()
        os.close(descriptor)
    assert not handover.path.exists()


def test_daemon_reports_a_launcher_that_never_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(devices.DEVICE_SOCKET_ENVIRONMENT, str(tmp_path / "nobody-listening"))
    with pytest.raises(DeviceAccessError, match="did not hand over the disc"):
        devices.open_device("/dev/sdb", writable=False)
    # The socket is single use and is not inherited by anything started later.
    assert devices.DEVICE_SOCKET_ENVIRONMENT not in os.environ


def test_unreadable_helper_reply_is_rejected() -> None:
    ours, theirs = socket.socketpair()
    try:
        theirs.sendall(b"not json\n")
        theirs.close()
        with pytest.raises(DeviceAccessError, match="unreadable reply"):
            devices._receive(ours)
    finally:
        ours.close()
    ours, theirs = socket.socketpair()
    try:
        theirs.sendall(json.dumps(["a", "list"]).encode())
        theirs.close()
        with pytest.raises(DeviceAccessError, match="unreadable reply"):
            devices._receive(ours)
    finally:
        ours.close()
