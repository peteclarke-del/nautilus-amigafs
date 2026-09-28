#!/usr/bin/python3 -I
"""Which physical discs AmigaFS may open, and the privileged helper that opens them.

This file has two roles and therefore imports nothing outside the standard
library.

Imported as ``amigafs.core.device_policy`` it describes the discs attached to
the host and decides which of them AmigaFS is prepared to touch.

Installed as ``/usr/libexec/amigafs/amigafs-device-helper`` and started through
``pkexec`` it applies the same policy as root, opens the one approved device and
passes the open descriptor back over the socket it was given as standard
output. The unprivileged filesystem daemon never gains a privilege; it receives
a single descriptor for a single disc.

The policy is deliberately narrow. A device is eligible only when it is a whole
disc, is removable or attached through USB or an SD/MMC host, has nothing
mounted from it and is not in use as swap or by the device mapper. The helper
additionally requires Amiga structures at the start of the disc, so it cannot be
used to obtain raw access to an ordinary PC disc.
"""

from __future__ import annotations

import array
import contextlib
import json
import os
import re
import socket
import stat
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

SYS_BLOCK = Path("/sys/block")
BY_ID = Path("/dev/disk/by-id")
PROC_MOUNTS = Path("/proc/mounts")
PROC_SWAPS = Path("/proc/swaps")
UDEV_DATA = Path("/run/udev/data")

SECTOR_BYTES = 512
RDB_SEARCH_BLOCKS = 16
HELPER_PROTOCOL = 1

#: Whole-disc kernel names that may hold Amiga media. Partitions, loop devices,
#: optical drives and device-mapper volumes are never offered.
_DISK_NAME = re.compile(r"^(sd[a-z]+|mmcblk\d+)$")
_ID_PREFERENCE = ("ata-", "scsi-", "mmc-", "wwn-", "usb-")
_VOLUME_SIGNATURES = (b"DOS", b"PFS", b"PDS", b"SFS")


class DevicePolicyError(Exception):
    """A device was refused. ``code`` is stable; ``message`` is for a person."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PhysicalDisc:
    """One eligible or refused whole disc, as the desktop presents it."""

    name: str
    device: str
    stable_path: str
    model: str
    size: int
    removable: bool
    usb: bool
    read_only: bool
    mounted: tuple[str, ...] = ()
    in_use: tuple[str, ...] = ()
    contents: str = ""
    refusal: str = ""
    refusal_code: str = ""
    accessible: bool = False
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return not self.refusal_code

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _is_usb(name: str, sys_block: Path) -> bool:
    try:
        return "/usb" in os.path.realpath(sys_block / name)
    except OSError:
        return False


def _is_mmc(name: str) -> bool:
    return name.startswith("mmcblk")


def stable_path(name: str, by_id: Path = BY_ID) -> str | None:
    """Return the most specific ``/dev/disk/by-id`` name for a whole disc."""

    try:
        links = [
            link
            for link in by_id.iterdir()
            if "-part" not in link.name and os.path.realpath(link) == f"/dev/{name}"
        ]
    except OSError:
        return None
    if not links:
        return None

    def rank(link: Path) -> tuple[int, str]:
        for index, prefix in enumerate(_ID_PREFERENCE):
            if link.name.startswith(prefix):
                return index, link.name
        return len(_ID_PREFERENCE), link.name

    return str(sorted(links, key=rank)[0])


def _decode_mount_field(value: str) -> str:
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n")


def _belongs_to(source: str, name: str) -> bool:
    if not source.startswith("/dev/"):
        return False
    try:
        resolved = os.path.realpath(source)
    except OSError:
        resolved = source
    return re.fullmatch(rf"/dev/{re.escape(name)}(p?\d+)?", resolved) is not None


def mounted_from(name: str, proc_mounts: Path = PROC_MOUNTS) -> tuple[str, ...]:
    """Return host mount points backed by this disc or one of its partitions."""

    found = []
    for line in _read_text(proc_mounts).splitlines():
        parts = line.split()
        if len(parts) >= 2 and _belongs_to(parts[0], name):
            found.append(_decode_mount_field(parts[1]))
    return tuple(found)


def _other_users(name: str, sys_block: Path, proc_swaps: Path) -> tuple[str, ...]:
    users: list[str] = []
    for line in _read_text(proc_swaps).splitlines()[1:]:
        parts = line.split()
        if parts and _belongs_to(parts[0], name):
            users.append("swap")
    roots = [sys_block / name]
    with contextlib.suppress(OSError):
        roots.extend(entry for entry in (sys_block / name).iterdir() if entry.name.startswith(name))
    for root in roots:
        try:
            holders = [entry.name for entry in (root / "holders").iterdir()]
        except OSError:
            holders = []
        users.extend(f"holder:{holder}" for holder in holders)
    return tuple(users)


def _model(name: str, sys_block: Path, udev_data: Path) -> str:
    numbers = _read_text(sys_block / name / "dev")
    record = _read_text(udev_data / f"b{numbers}") if numbers else ""
    properties = dict(
        line[2:].split("=", 1)
        for line in record.splitlines()
        if line.startswith("E:") and "=" in line
    )
    model = properties.get("ID_MODEL", "").replace("_", " ").strip()
    if model:
        return model
    device = sys_block / name / "device"
    vendor = _read_text(device / "vendor")
    model = _read_text(device / "model") or _read_text(device / "name")
    text = " ".join(part for part in (vendor, model) if part and part.lower() != "generic")
    return text or model or name


def amiga_evidence(head: bytes) -> str:
    """Describe the Amiga structures at the start of a disc, or return ``""``."""

    for block in range(RDB_SEARCH_BLOCKS):
        start = block * SECTOR_BYTES
        if head[start : start + 4] == b"RDSK":
            return "rdb"
    signature = head[:4]
    if len(signature) == 4 and signature[:3] in _VOLUME_SIGNATURES and signature[3] < 8:
        return "volume"
    return ""


def _describe(
    name: str,
    sys_block: Path,
    by_id: Path,
    proc_mounts: Path,
    proc_swaps: Path,
    udev_data: Path,
    dev_root: Path,
) -> PhysicalDisc:
    device = str(dev_root / name)
    try:
        size = int(_read_text(sys_block / name / "size") or 0) * SECTOR_BYTES
    except ValueError:
        size = 0
    removable = _read_text(sys_block / name / "removable") == "1"
    usb = _is_usb(name, sys_block)
    read_only = _read_text(sys_block / name / "ro") == "1"
    mounted = mounted_from(name, proc_mounts)
    in_use = _other_users(name, sys_block, proc_swaps)
    code = ""
    refusal = ""
    if not (removable or usb or _is_mmc(name)):
        code = "internal"
        refusal = "This is one of the computer's own discs, which AmigaFS never opens."
    elif not size:
        code = "no-media"
        refusal = "There is no disc or card in this drive."
    elif mounted:
        code = "mounted"
        refusal = "Part of this disc is mounted by Linux: " + ", ".join(mounted)
    elif in_use:
        code = "in-use"
        refusal = "This disc is in use by the system."
    return PhysicalDisc(
        name=name,
        device=device,
        stable_path=stable_path(name, by_id) or device,
        model=_model(name, sys_block, udev_data),
        size=size,
        removable=removable,
        usb=usb,
        read_only=read_only,
        mounted=mounted,
        in_use=in_use,
        refusal=refusal,
        refusal_code=code,
        accessible=os.access(device, os.R_OK),
    )


def list_physical_discs(
    *,
    sys_block: Path = SYS_BLOCK,
    by_id: Path = BY_ID,
    proc_mounts: Path = PROC_MOUNTS,
    proc_swaps: Path = PROC_SWAPS,
    udev_data: Path = UDEV_DATA,
    dev_root: Path = Path("/dev"),
    include_refused: bool = False,
) -> list[PhysicalDisc]:
    """Describe the removable and USB discs attached to this host."""

    try:
        names = sorted(entry.name for entry in sys_block.iterdir() if _DISK_NAME.match(entry.name))
    except OSError:
        return []
    discs = []
    for name in names:
        disc = _describe(name, sys_block, by_id, proc_mounts, proc_swaps, udev_data, dev_root)
        if disc.refusal_code == "internal":
            continue
        if disc.eligible or include_refused:
            discs.append(disc)
    return discs


def resolve_disc(
    selected: str | os.PathLike[str],
    *,
    sys_block: Path = SYS_BLOCK,
    by_id: Path = BY_ID,
    proc_mounts: Path = PROC_MOUNTS,
    proc_swaps: Path = PROC_SWAPS,
    udev_data: Path = UDEV_DATA,
    dev_root: Path = Path("/dev"),
) -> PhysicalDisc:
    """Return the eligible disc a path names, or explain why it is refused."""

    text = os.fspath(selected)
    try:
        resolved = os.path.realpath(text)
        details = os.stat(resolved)
    except OSError as exc:
        raise DevicePolicyError("missing", f"{text} cannot be inspected: {exc.strerror}") from exc
    if not stat.S_ISBLK(details.st_mode):
        raise DevicePolicyError("not-block", f"{text} is not a block device.")
    name = os.path.basename(resolved)
    if os.path.dirname(resolved) != str(dev_root) or not _DISK_NAME.match(name):
        raise DevicePolicyError(
            "not-whole-disc",
            f"{text} is not a whole removable disc. Partitions and virtual devices are refused.",
        )
    if not (sys_block / name).is_dir():
        raise DevicePolicyError("unknown", f"{text} is not a disc the kernel describes.")
    disc = _describe(name, sys_block, by_id, proc_mounts, proc_swaps, udev_data, dev_root)
    if disc.refusal_code:
        raise DevicePolicyError(disc.refusal_code, disc.refusal)
    return disc


def open_approved(
    selected: str, *, writable: bool, require_amiga: bool = True
) -> tuple[int, PhysicalDisc]:
    """Apply the policy, open the disc and re-check what was actually opened."""

    disc = resolve_disc(selected)
    if writable and disc.read_only:
        raise DevicePolicyError("read-only", "The disc or its adapter is write-protected.")
    flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOCTTY | os.O_NONBLOCK
    # O_EXCL on a block device fails while the kernel holds it for a mount,
    # which closes the window between the policy check and the open.
    try:
        descriptor = os.open(disc.device, flags | os.O_EXCL)
    except OSError as exc:
        raise DevicePolicyError(
            "open-failed", f"{disc.device} could not be opened: {exc.strerror}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        expected = os.stat(disc.device)
        if not stat.S_ISBLK(opened.st_mode) or opened.st_rdev != expected.st_rdev:
            raise DevicePolicyError("changed", "The device changed while it was being opened.")
        if mounted_from(disc.name):
            raise DevicePolicyError(
                "mounted", "Part of this disc was mounted while it was being opened."
            )
        if require_amiga:
            head = os.pread(descriptor, RDB_SEARCH_BLOCKS * SECTOR_BYTES, 0)
            if not amiga_evidence(head):
                raise DevicePolicyError(
                    "not-amiga",
                    "No Rigid Disk Block or Amiga volume was found at the start of this disc.",
                )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, disc


def send_reply(channel: socket.socket, payload: dict[str, object], descriptor: int | None) -> None:
    data = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    ancillary = []
    if descriptor is not None:
        ancillary.append(
            (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [descriptor]).tobytes())
        )
    channel.sendmsg([data], ancillary)


def helper_main(argv: list[str]) -> int:
    """Entry point when run as root through pkexec."""

    import argparse

    parser = argparse.ArgumentParser(prog="amigafs-device-helper", add_help=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--mode", choices=("ro", "rw"), default="ro")
    parser.add_argument(
        "--allow-blank",
        action="store_true",
        help="permit a disc with no Amiga structures (for writing an image to it)",
    )
    arguments = parser.parse_args(argv)
    try:
        details = os.fstat(1)
    except OSError:
        return 2
    if not stat.S_ISSOCK(details.st_mode):
        sys.stderr.write("amigafs-device-helper must be started by AmigaFS.\n")
        return 2
    channel = socket.socket(fileno=os.dup(1))
    try:
        try:
            descriptor, disc = open_approved(
                arguments.device,
                writable=arguments.mode == "rw",
                require_amiga=not arguments.allow_blank,
            )
        except DevicePolicyError as exc:
            send_reply(
                channel,
                {
                    "protocol": HELPER_PROTOCOL,
                    "ok": False,
                    "code": exc.code,
                    "message": exc.message,
                },
                None,
            )
            return 1
        try:
            send_reply(
                channel,
                {
                    "protocol": HELPER_PROTOCOL,
                    "ok": True,
                    "device": disc.device,
                    "stable_path": disc.stable_path,
                    "size": disc.size,
                    "writable": arguments.mode == "rw",
                },
                descriptor,
            )
        finally:
            os.close(descriptor)
        return 0
    finally:
        channel.close()


__all__ = [
    "DevicePolicyError",
    "HELPER_PROTOCOL",
    "PhysicalDisc",
    "amiga_evidence",
    "helper_main",
    "list_physical_discs",
    "mounted_from",
    "open_approved",
    "resolve_disc",
    "send_reply",
    "stable_path",
]


if __name__ == "__main__":
    raise SystemExit(helper_main(sys.argv[1:]))
