"""Opening physical Amiga discs without giving the filesystem daemon privileges.

A disc is opened in one of three ways, tried in this order:

1. from a descriptor handed over by the desktop launcher, which obtained it
   inside the graphical session where a polkit prompt can be shown;
2. directly, when the account already has access to the device;
3. through the polkit helper, which opens the disc as root and passes back the
   single open descriptor.

Every route applies :mod:`amigafs.core.device_policy`.
"""

from __future__ import annotations

import array
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import time
from contextlib import suppress
from pathlib import Path

from amigafs.core import device_policy
from amigafs.core.device_policy import DevicePolicyError, PhysicalDisc
from amigafs.errors import AmigaFSError, DeviceAccessError
from amigafs.i18n import _

DEVICE_SOCKET_ENVIRONMENT = "AMIGAFS_DEVICE_SOCKET"
HELPER_NAME = "amigafs-device-helper"
HELPER_LOCATIONS = (
    Path("/usr/libexec/amigafs") / HELPER_NAME,
    Path("/usr/lib/amigafs") / HELPER_NAME,
)
HELPER_TIMEOUT = 5 * 60.0
HANDOVER_TIMEOUT = 60.0
_REPLY_BYTES = 8192


def _policy_message(exc: DevicePolicyError) -> str:
    messages = {
        "internal": _("This is one of the computer's own discs, which AmigaFS never opens."),
        "no-media": _("There is no disc or card in this drive."),
        "mounted": _("Part of this disc is mounted by Linux. Unmount it there first."),
        "in-use": _("This disc is in use by the system."),
        "not-block": _("The selected path is not a physical disc."),
        "not-whole-disc": _(
            "Only whole removable discs can be opened; partitions and virtual devices are refused."
        ),
        "read-only": _("The disc or its adapter is write-protected."),
        "not-amiga": _("No Rigid Disk Block or Amiga volume was found at the start of this disc."),
        "changed": _("The device changed while it was being opened."),
    }
    return messages.get(exc.code, exc.message)


def is_block_device(path: str | Path) -> bool:
    try:
        return stat.S_ISBLK(Path(path).stat().st_mode)
    except OSError:
        return False


def describe_disc(selected: str | Path) -> PhysicalDisc:
    """Return the policy description of one disc, translating a refusal."""

    try:
        return device_policy.resolve_disc(selected)
    except DevicePolicyError as exc:
        raise DeviceAccessError(_policy_message(exc)) from exc


def list_discs(*, include_refused: bool = False) -> list[PhysicalDisc]:
    return device_policy.list_physical_discs(include_refused=include_refused)


def trusted_helper() -> Path | None:
    """Return the installed helper only when an unprivileged user cannot alter it."""

    for candidate in HELPER_LOCATIONS:
        try:
            details = candidate.stat()
        except OSError:
            continue
        if (
            stat.S_ISREG(details.st_mode)
            and details.st_uid == 0
            and not details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            and details.st_mode & stat.S_IXUSR
        ):
            return candidate
    return None


def _receive(channel: socket.socket) -> tuple[dict[str, object], int | None]:
    descriptors = array.array("i")
    data, ancillary, _flags, _address = channel.recvmsg(
        _REPLY_BYTES, socket.CMSG_LEN(struct.calcsize("i") * 4)
    )
    for level, kind, payload in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            usable = len(payload) - len(payload) % descriptors.itemsize
            descriptors.frombytes(payload[:usable])
    received = list(descriptors)
    for extra in received[1:]:
        with suppress(OSError):
            os.close(extra)
    descriptor = received[0] if received else None
    try:
        reply = json.loads(data.decode("utf-8")) if data else {}
        if not isinstance(reply, dict):
            raise ValueError("reply is not an object")
    except (UnicodeDecodeError, ValueError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise DeviceAccessError(_("The device helper returned an unreadable reply.")) from exc
    return reply, descriptor


def _verify_received(descriptor: int, selected: str | Path, *, writable: bool) -> None:
    opened = os.fstat(descriptor)
    if not stat.S_ISBLK(opened.st_mode):
        raise DeviceAccessError(_("The device helper returned something that is not a disc."))
    expected = os.stat(os.path.realpath(os.fspath(selected)))
    if not stat.S_ISBLK(expected.st_mode) or expected.st_rdev != opened.st_rdev:
        raise DeviceAccessError(_("The device helper returned a different disc."))
    import fcntl

    access = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
    if writable and access != os.O_RDWR:
        raise DeviceAccessError(_("The device helper returned a read-only descriptor."))
    if not writable and access != os.O_RDONLY:
        raise DeviceAccessError(_("The device helper returned more access than was requested."))


def open_through_helper(
    selected: str | Path,
    *,
    writable: bool,
    allow_blank: bool = False,
    helper: Path | None = None,
    launcher: str | None = None,
) -> int:
    """Ask the polkit helper to open one disc and return the descriptor it passes back."""

    program = helper or trusted_helper()
    if program is None:
        raise DeviceAccessError(
            _(
                "This account cannot open the disc, and the AmigaFS device helper is not "
                "installed. Install the AmigaFS system package, or give the account access to "
                "the device."
            )
        )
    elevate = launcher or shutil.which("pkexec")
    if elevate is None:
        raise DeviceAccessError(_("polkit ('pkexec') is required to open a physical disc."))
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    command = [
        elevate,
        str(program),
        "--device",
        os.path.realpath(os.fspath(selected)),
        "--mode",
        "rw" if writable else "ro",
    ]
    if allow_blank:
        command.append("--allow-blank")
    try:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=theirs.fileno(),
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except OSError as exc:
            raise DeviceAccessError(
                _("Could not start the device helper: {error}").format(error=exc)
            ) from exc
        theirs.close()
        ours.settimeout(HELPER_TIMEOUT)
        try:
            reply, descriptor = _receive(ours)
        except (TimeoutError, OSError) as exc:
            process.kill()
            process.wait()
            raise DeviceAccessError(_("The device helper did not answer.")) from exc
        try:
            _stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            _stdout, stderr = process.communicate()
        if not reply:
            if descriptor is not None:
                os.close(descriptor)
            if process.returncode in {126, 127}:
                raise DeviceAccessError(_("Permission to open the disc was not granted."))
            detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise DeviceAccessError(
                _("The device helper failed: {detail}").format(
                    detail=detail[-1] if detail else process.returncode
                )
            )
        if not reply.get("ok"):
            if descriptor is not None:
                os.close(descriptor)
            raise DeviceAccessError(
                _policy_message(
                    DevicePolicyError(str(reply.get("code", "")), str(reply.get("message", "")))
                )
            )
        if descriptor is None:
            raise DeviceAccessError(_("The device helper did not pass back an open disc."))
        try:
            _verify_received(descriptor, selected, writable=writable)
        except BaseException:
            os.close(descriptor)
            raise
        os.set_inheritable(descriptor, False)
        return descriptor
    finally:
        with suppress(OSError):
            theirs.close()
        ours.close()


def _open_from_launcher(socket_path: str, selected: str | Path, *, writable: bool) -> int:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(HANDOVER_TIMEOUT)
        try:
            channel.connect(socket_path)
            reply, descriptor = _receive(channel)
        except (TimeoutError, OSError) as exc:
            raise DeviceAccessError(
                _("The desktop launcher did not hand over the disc: {error}").format(error=exc)
            ) from exc
    if not reply.get("ok") or descriptor is None:
        if descriptor is not None:
            os.close(descriptor)
        raise DeviceAccessError(
            str(reply.get("message") or _("The desktop launcher could not open the disc."))
        )
    try:
        _verify_received(descriptor, selected, writable=writable)
    except BaseException:
        os.close(descriptor)
        raise
    os.set_inheritable(descriptor, False)
    return descriptor


def open_device(
    selected: str | Path,
    *,
    writable: bool,
    allow_blank: bool = False,
    use_helper: bool = True,
) -> int:
    """Return an open descriptor for one policy-approved physical disc."""

    handed_over = os.environ.pop(DEVICE_SOCKET_ENVIRONMENT, None)
    if handed_over:
        return _open_from_launcher(handed_over, selected, writable=writable)
    try:
        disc = device_policy.resolve_disc(selected)
    except DevicePolicyError as exc:
        raise DeviceAccessError(_policy_message(exc)) from exc
    wanted = os.R_OK | (os.W_OK if writable else 0)
    if os.access(disc.device, wanted):
        try:
            descriptor, _disc = device_policy.open_approved(
                disc.device, writable=writable, require_amiga=not allow_blank
            )
        except DevicePolicyError as exc:
            raise DeviceAccessError(_policy_message(exc)) from exc
        return descriptor
    if not use_helper:
        raise DeviceAccessError(_("This account does not have access to the disc."))
    return open_through_helper(disc.device, writable=writable, allow_blank=allow_blank)


class DescriptorHandover:
    """Pass one open descriptor to a daemon started outside this process tree."""

    def __init__(self, directory: Path, name: str) -> None:
        self.path = directory / name
        self.path.unlink(missing_ok=True)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)
        try:
            self._listener.bind(str(self.path))
        finally:
            os.umask(previous)
        self._listener.listen(1)
        self._listener.setblocking(False)

    def serve_once(self, descriptor: int, *, timeout: float = 0.0) -> bool:
        """Hand the descriptor to the next caller owned by this user, if one is waiting."""

        deadline = time.monotonic() + timeout
        while True:
            try:
                connection, _address = self._listener.accept()
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
        with connection:
            credentials = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _pid, uid, _gid = struct.unpack("3i", credentials)
            if uid != os.getuid():
                return False
            device_policy.send_reply(
                connection, {"protocol": device_policy.HELPER_PROTOCOL, "ok": True}, descriptor
            )
        return True

    def close(self) -> None:
        self._listener.close()
        self.path.unlink(missing_ok=True)


def require_device_error(exc: Exception) -> AmigaFSError:
    return exc if isinstance(exc, AmigaFSError) else DeviceAccessError(str(exc))


__all__ = [
    "DEVICE_SOCKET_ENVIRONMENT",
    "DescriptorHandover",
    "HELPER_LOCATIONS",
    "HELPER_NAME",
    "describe_disc",
    "is_block_device",
    "list_discs",
    "open_device",
    "open_through_helper",
    "trusted_helper",
]
