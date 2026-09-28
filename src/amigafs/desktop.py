"""Non-blocking desktop helpers for Nautilus actions."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import TypeVar
from urllib.parse import unquote, urlparse

from amigafs.core import (
    IntegrityReport,
    RepairPlan,
    ResolvedImage,
    apply_repairs,
    create_floppy_image,
    create_hard_disc_image,
    plan_repairs,
    plan_repairs_from_report,
    resolve_image,
    validate_image_report,
)
from amigafs.core.devices import (
    DEVICE_SOCKET_ENVIRONMENT,
    DescriptorHandover,
    list_discs,
    open_device,
)
from amigafs.core.disc_transfer import read_disc, write_disc
from amigafs.core.formats import floppy_reference
from amigafs.errors import AmigaFSError, OperationCancelled
from amigafs.file_forge import open_in_file_forge
from amigafs.greaseweazle import (
    DENSITY_CHOICES,
    detected_drives,
    read_floppy,
    responsive_command,
    write_floppy,
)
from amigafs.i18n import _
from amigafs.mounts import (
    is_mounted,
    mount_at,
    mount_for_image,
    runtime_root,
    wait_for_mount_shutdown,
)
from amigafs.preferences import ensure_mount_root, mount_location, mount_root, set_mount_location
from amigafs.privacy import safe_name, safe_user_message
from amigafs.recovery import (
    KIND_WORKSPACE,
    pending_recovery,
    recover_image,
    salvage_workspace,
)
from amigafs.retention import cleanup_retained_state
from amigafs.safe_paths import ensure_private_directory

MOUNT_TIMEOUT = 15.0
CONTAINER_MOUNT_TIMEOUT = 330.0
WRITABLE_MOUNT_TIMEOUT = 300.0
FLOPPY_MOUNT_TIMEOUT = 35 * 60.0
SHUTDOWN_TIMEOUT = 120.0
CONTAINER_SHUTDOWN_TIMEOUT = 10 * 60.0
FLOPPY_SHUTDOWN_TIMEOUT = 35 * 60.0
_T = TypeVar("_T")


def _mount_timeout(image: ResolvedImage, *, read_write: bool) -> float:
    if image.kind == "physical-floppy":
        return FLOPPY_MOUNT_TIMEOUT
    if image.container is not None:
        return CONTAINER_MOUNT_TIMEOUT
    return WRITABLE_MOUNT_TIMEOUT if read_write else MOUNT_TIMEOUT


def shutdown_timeout(kind: str | None) -> float:
    """Return how long a writable daemon may take to write back and validate."""

    if kind == "physical-floppy":
        return FLOPPY_SHUTDOWN_TIMEOUT
    if kind in {"compressed-image", "flux-image"}:
        return CONTAINER_SHUTDOWN_TIMEOUT
    return SHUTDOWN_TIMEOUT


_DESKTOP_ENVIRONMENT = (
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "XDG_STATE_HOME",
)


def _desktop_environment() -> dict[str, str]:
    """Return only the session variables a detached AmigaFS child needs."""

    environment = {
        name: value for name in _DESKTOP_ENVIRONMENT if (value := os.environ.get(name)) is not None
    }
    environment["AMIGAFS_DESKTOP_MOUNT"] = "1"
    return environment


def mountpoint_for_image(image_path: str | Path | ResolvedImage) -> Path:
    image = image_path if isinstance(image_path, ResolvedImage) else resolve_image(image_path)
    identity = str(image.primary_path).encode("utf-8", "surrogateescape")
    digest = hashlib.sha256(identity).hexdigest()[:10]
    label = Path(image.name).stem if image.container != "greaseweazle" else image.name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip(".-") or "image"
    return mount_root() / f"{stem[:48]}-{digest}"


def _unit_for_mountpoint(mountpoint: Path) -> str:
    digest = hashlib.sha256(str(mountpoint).encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return f"amigafs-mount-{digest}.service"


def _systemd_user_available() -> bool:
    if os.environ.get("AMIGAFS_NO_SYSTEMD") == "1":
        return False
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _systemd_mount_command(
    unit: str,
    command: list[str],
    *,
    device_socket: Path | None = None,
    stop_timeout: float = 30.0,
) -> list[str]:
    result = [
        "systemd-run",
        "--user",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        "--service-type=exec",
        "--property=KillMode=mixed",
        "--property=KillSignal=SIGINT",
        # Stopping the service is a clean unmount, which may have to write a
        # working copy back before it is safe to exit.
        f"--property=TimeoutStopSec={max(30, int(stop_timeout))}s",
        "--setenv=AMIGAFS_DESKTOP_MOUNT=1",
    ]
    for name in (
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "WAYLAND_DISPLAY",
    ):
        value = os.environ.get(name)
        if value is not None:
            result.append(f"--setenv={name}={value}")
    if device_socket is not None:
        result.append(f"--setenv={DEVICE_SOCKET_ENVIRONMENT}={device_socket}")
    return [*result, "--", *command]


def _unit_active(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", unit],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _unit_log_line(unit: str) -> str:
    result = subprocess.run(
        ["journalctl", "--user", "--unit", unit, "--lines=1", "--no-pager", "--output=cat"],
        check=False,
        capture_output=True,
        text=True,
    )
    return (
        safe_user_message(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else ""
    )


def _stop_unit(unit: str) -> None:
    subprocess.run(
        ["systemctl", "--user", "stop", unit],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _notify(summary: str, body: str, *, error: bool = False) -> None:
    command = shutil.which("notify-send")
    if command is None:
        return
    urgency = "critical" if error else "normal"
    subprocess.run(
        [command, f"--urgency={urgency}", summary, safe_user_message(body)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _open_folder(path: Path) -> None:
    subprocess.Popen(
        ["gio", "open", path.as_uri()],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _dialog_report(report: IntegrityReport) -> str:
    """Wrap a validation report for a compact, readable desktop dialog."""

    wrapped: list[str] = []
    for line in report.format_text().splitlines():
        wrapped.extend(
            textwrap.wrap(
                line,
                width=88,
                subsequent_indent="  " if line.startswith("- ") else "",
                replace_whitespace=False,
            )
            or [""]
        )
    return "\n".join(wrapped)


def _show_desktop_message(title: str, message: str, *, error: bool = False) -> None:
    """Show a finite one-button result dialog, falling back to a notification."""

    if error:
        message = safe_user_message(message)
    dialog = shutil.which("zenity")
    if dialog is None:
        _notify(title, message, error=error)
        return
    subprocess.run(
        [
            dialog,
            "--error" if error else "--info",
            "--no-markup",
            f"--title={title}",
            f"--text={message}",
            f"--ok-label={_('Close')}",
            "--width=560",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _run_with_progress(
    title: str,
    message: str,
    operation: Callable[[Callable[[], bool]], _T],
) -> _T:
    """Run work beside a cancellable pulse dialog using cooperative boundaries."""

    dialog = shutil.which("zenity")
    if dialog is None:
        return operation(lambda: False)
    cancelled = threading.Event()
    try:
        progress = subprocess.Popen(
            [
                dialog,
                "--progress",
                "--pulsate",
                "--auto-close",
                f"--title={title}",
                f"--text={message}",
                f"--cancel-label={_('Cancel safely')}",
                "--width=520",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return operation(lambda: False)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="amigafs-operation") as executor:
        future = executor.submit(operation, cancelled.is_set)
        while not future.done():
            if progress.poll() is not None:
                cancelled.set()
                break
            time.sleep(0.05)
        try:
            return future.result()
        finally:
            if progress.poll() is None:
                if progress.stdin is not None:
                    with suppress(BrokenPipeError, OSError):
                        progress.stdin.write("100\n")
                        progress.stdin.close()
                with suppress(subprocess.TimeoutExpired):
                    progress.wait(timeout=2)
                if progress.poll() is None:
                    progress.terminate()


def _run_with_reported_progress(
    title: str,
    message: str,
    operation: Callable[[Callable[[int, str], None]], _T],
) -> _T:
    """Run non-cancellable transactional work with determinate progress updates."""

    dialog = shutil.which("zenity")
    if dialog is None:
        return operation(lambda _percent, _message: None)
    try:
        progress = subprocess.Popen(
            [
                dialog,
                "--progress",
                "--auto-close",
                "--no-cancel",
                "--percentage=0",
                f"--title={title}",
                f"--text={message}",
                "--width=560",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return operation(lambda _percent, _message: None)

    def update(percent: int, detail: str) -> None:
        if progress.poll() is not None or progress.stdin is None:
            return
        with suppress(BrokenPipeError, OSError):
            progress.stdin.write(f"#{detail}\n{max(0, min(100, percent))}\n")
            progress.stdin.flush()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="amigafs-repair") as executor:
        future = executor.submit(operation, update)
        while not future.done():
            time.sleep(0.05)
        try:
            return future.result()
        finally:
            if progress.poll() is None:
                if progress.stdin is not None:
                    with suppress(BrokenPipeError, OSError):
                        progress.stdin.close()
                with suppress(subprocess.TimeoutExpired):
                    progress.wait(timeout=2)
                if progress.poll() is None:
                    progress.terminate()


def _show_validation_report(
    name: str, report: IntegrityReport, *, offer_repair: bool = False
) -> bool | None:
    """Show complete findings and return whether the user selected repair."""

    dialog = shutil.which("zenity")
    if dialog is None:
        return None
    content = _dialog_report(report)
    line_count = len(content.splitlines())
    height = min(480, max(240, 145 + line_count * 22))
    arguments = [
        dialog,
        "--text-info",
        f"--title={_('AmigaFS validation — {name}').format(name=name)}",
        "--width=680",
        f"--height={height}",
        f"--ok-label={_('Repair…') if offer_repair else _('Close')}",
    ]
    if offer_repair:
        arguments.append(f"--cancel-label={_('Cancel')}")
    else:
        arguments.append("--no-cancel")
    result = subprocess.run(
        arguments,
        input=content,
        text=True,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return offer_repair and result.returncode == 0


def background_mount(
    image_path: str | Path,
    *,
    open_folder: bool = True,
    notify: bool = True,
    timeout: float | None = None,
    read_write: bool = False,
    progress: Callable[[int, str], None] | None = None,
) -> Path:
    """Start a detached foreground mount process and wait until it is ready."""

    report = progress or (lambda _percent, _detail: None)
    report(5, _("Inspecting the selected image…"))
    cleanup_retained_state()
    image = resolve_image(image_path)
    if read_write and not image.capabilities.mount_read_write:
        raise AmigaFSError(_("Read-write mounting is not supported for this image format."))
    if timeout is None:
        timeout = _mount_timeout(image, read_write=read_write)
    report(15, _("Preparing the mount location…"))
    mountpoint = mountpoint_for_image(image)
    reference = (
        f"floppy:{image.drive}" if image.kind == "physical-floppy" else str(image.primary_path)
    )
    root = runtime_root()
    ensure_private_directory(root, anchor=root.parent)
    lock_path = root / f".{mountpoint.name}.lock"
    log_path = root / f"{mountpoint.name}.log"

    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        mounted_image = mount_for_image(image)
        if mounted_image is not None:
            mountpoint = Path(mounted_image.mountpoint)
        else:
            ensured_root = ensure_mount_root()
            if mountpoint.parent != ensured_root:
                raise AmigaFSError(_("The selected AmigaFS mount root changed unexpectedly."))
            ensure_private_directory(mountpoint, anchor=ensured_root)
            cleanup_stale_mountpoint(mountpoint)
        existing = mount_at(mountpoint)
        if existing is not None and mounted_image is None:
            raise AmigaFSError(
                _(
                    "The image's mount location is occupied by a different file identity. "
                    "Unmount that location before mounting the replacement image."
                )
            )
        handover: DescriptorHandover | None = None
        descriptor: int | None = None
        if existing is None and image.is_device:
            # The disc is opened here, inside the graphical session, where a
            # polkit prompt can be shown. The daemon receives only the open
            # descriptor.
            report(20, _("Requesting access to the disc…"))
            descriptor = open_device(image.primary_path, writable=read_write)
            handover = DescriptorHandover(root, f".{mountpoint.name}.device")
        try:
            if existing is None:
                _start_mount_service(
                    image,
                    reference,
                    mountpoint,
                    log_path,
                    read_write=read_write,
                    timeout=timeout,
                    report=report,
                    handover=handover,
                    descriptor=descriptor,
                )
        finally:
            if handover is not None:
                handover.close()
            if descriptor is not None:
                os.close(descriptor)

    report(90, _("Opening the mounted image in Files…"))
    if open_folder:
        _open_folder(mountpoint)
    if notify:
        mode = _("read-write") if read_write else _("read-only")
        _notify(
            _("AmigaFS image mounted"),
            _("{image} is available in Files ({mode}).").format(image=image.name, mode=mode),
        )
    report(100, _("Mount complete."))
    return mountpoint


def _start_mount_service(
    image: ResolvedImage,
    reference: str,
    mountpoint: Path,
    log_path: Path,
    *,
    read_write: bool,
    timeout: float,
    report: Callable[[int, str], None],
    handover: DescriptorHandover | None,
    descriptor: int | None,
) -> None:
    """Launch the mount daemon and wait for the kernel to confirm the mount."""

    report(25, _("Starting the AmigaFS mount service…"))
    command = [sys.executable, "-m", "amigafs.cli", "mount"]
    if read_write:
        command.append("--read-write")
    command.extend((reference, str(mountpoint)))
    process: subprocess.Popen[bytes] | None = None
    unit: str | None = None
    device_socket = handover.path if handover is not None else None
    if _systemd_user_available():
        unit = _unit_for_mountpoint(mountpoint)
        launch = subprocess.run(
            _systemd_mount_command(
                unit,
                command,
                device_socket=device_socket,
                stop_timeout=shutdown_timeout(image.kind) if read_write else 30.0,
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        if launch.returncode:
            detail = launch.stderr.strip() or _("systemd-run failed")
            raise AmigaFSError(
                _("Could not start the AmigaFS user service: {detail}").format(detail=detail)
            )
    else:
        environment = _desktop_environment()
        if device_socket is not None:
            environment[DEVICE_SOCKET_ENVIRONMENT] = str(device_socket)
        with log_path.open("ab") as log:
            os.fchmod(log.fileno(), 0o600)
            process = subprocess.Popen(
                command,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    deadline = time.monotonic() + timeout
    started_at = time.monotonic()
    last_percent = -1
    handed_over = handover is None
    while time.monotonic() < deadline:
        if not handed_over and handover is not None and descriptor is not None:
            handed_over = handover.serve_once(descriptor)
        if mount_for_image(image) is not None:
            break
        if process is not None and process.poll() is not None:
            detail = _last_log_line(log_path)
            raise AmigaFSError(
                detail
                or _("Mount process exited with status {status}.").format(status=process.returncode)
            )
        if unit is not None and not _unit_active(unit):
            detail = _unit_log_line(unit)
            raise AmigaFSError(detail or _("The AmigaFS user service exited before mounting."))
        elapsed = time.monotonic() - started_at
        percent = 30 + min(55, int(elapsed / timeout * 55))
        if percent != last_percent:
            report(
                percent,
                _("Waiting for {image} to mount…").format(image=image.name),
            )
            last_percent = percent
        time.sleep(0.1)
    else:
        if process is not None:
            process.terminate()
        if unit is not None:
            _stop_unit(unit)
        raise AmigaFSError(_("Timed out mounting {image}.").format(image=image.name))


def _last_log_line(log_path: Path) -> str:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return safe_user_message(lines[-1]) if lines else ""


def _display_reference(reference: str | Path) -> str:
    drive = floppy_reference(reference)
    if drive is not None:
        return _("Floppy drive {drive}").format(drive=drive)
    return safe_name(Path(reference).name)


def desktop_mount(image_path: str | Path, *, read_write: bool = False) -> int:
    floppy = floppy_reference(image_path) is not None
    image: str | Path = image_path if floppy else Path(image_path).expanduser()
    name = _display_reference(image_path)
    mode = _("read-write") if read_write else _("read-only")
    try:
        mountpoint = _run_with_reported_progress(
            _("Opening AmigaFS image"),
            _("Inspecting {image}…").format(image=name),
            lambda progress: background_mount(
                image,
                open_folder=False,
                notify=False,
                read_write=read_write,
                progress=progress,
            ),
        )
    except AmigaFSError as exc:
        _show_desktop_message(_("AmigaFS mount failed"), str(exc), error=True)
        raise
    _open_folder(mountpoint)
    _show_desktop_message(
        _("AmigaFS image mounted"),
        _("{image} is available in Files at {mountpoint} ({mode}).").format(
            image=name,
            mountpoint=mountpoint,
            mode=mode,
        ),
    )
    return 0


def _expand_image_path(reference: str | Path) -> Path:
    if "\0" in os.fspath(reference):
        raise AmigaFSError(_("The image path contains an invalid path character (NUL)."))
    try:
        return Path(reference).expanduser()
    except (KeyError, RuntimeError) as exc:
        raise AmigaFSError(_("The image path names an unknown user account.")) from exc


def local_image_reference(reference: str | Path) -> Path:
    """Convert a desktop path, file URI or AmigaFS URI to one local image."""

    if isinstance(reference, Path):
        return _expand_image_path(reference)
    try:
        parsed = urlparse(reference)
    except ValueError as exc:
        raise AmigaFSError(_("The image URI is malformed.")) from exc
    if not parsed.scheme:
        return _expand_image_path(reference)
    if parsed.scheme not in {"file", "amigafs"}:
        raise AmigaFSError(_("Unsupported image URI scheme: {scheme}").format(scheme=parsed.scheme))
    if parsed.netloc not in {"", "localhost"}:
        raise AmigaFSError(_("AmigaFS can open only local image URIs."))
    if parsed.params or parsed.query or parsed.fragment or not parsed.path:
        raise AmigaFSError(_("The image URI must contain one unambiguous local path."))
    path = unquote(parsed.path)
    return _expand_image_path(path)


def desktop_open(image_references: list[str]) -> int:
    """Open local desktop/MIME references as safe read-only mounts."""

    for reference in image_references:
        try:
            image_path = local_image_reference(reference)
        except AmigaFSError as exc:
            _notify(_("AmigaFS open failed"), str(exc), error=True)
            raise
        desktop_mount(image_path, read_write=False)
    return 0


def desktop_open_file_forge(image_path: str | Path) -> int:
    """Hand a local image to an installed File Forge desktop launcher."""

    try:
        open_in_file_forge(image_path)
    except AmigaFSError as exc:
        _show_desktop_message(_("Could not open Amiga File Forge"), str(exc), error=True)
        raise
    return 0


def _require_zenity(fallback: str) -> str:
    dialog = shutil.which("zenity")
    if dialog is None:
        raise AmigaFSError(fallback)
    return dialog


def _choose_floppy_drive(dialog: str, title: str, prompt: str) -> str | None:
    """Detect drives holding a disk and let the user pick one."""

    command = responsive_command()
    if command is None:
        detail = _("No responsive Greaseweazle device is currently connected.")
        _show_desktop_message(_("Greaseweazle unavailable"), detail, error=True)
        raise AmigaFSError(detail)
    drives = _run_with_reported_progress(
        _("Detecting physical drives"),
        _("Checking the connected Greaseweazle drives…"),
        lambda progress: detected_drives(command=command, progress=progress),
    )
    if not drives:
        detail = _(
            "No physical drive with an indexed floppy was detected. Insert the "
            "disk, check the drive cable and power, then try again."
        )
        _show_desktop_message(_("No physical drive detected"), detail, error=True)
        raise AmigaFSError(detail)
    selection = subprocess.run(
        [
            dialog,
            "--forms",
            f"--title={title}",
            f"--text={prompt}",
            f"--add-combo={_('Physical drive')}",
            f"--combo-values={'|'.join(drives)}",
            f"--ok-label={_('Continue')}",
            f"--cancel-label={_('Cancel')}",
            "--width=560",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if selection.returncode != 0:
        return None
    drive = selection.stdout.strip() or drives[0]
    if drive not in drives:
        raise AmigaFSError(_("The drive-selection dialog returned an invalid response."))
    return drive


def _confirm(dialog: str, title: str, text: str, action: str) -> bool:
    confirmation = subprocess.run(
        [
            dialog,
            "--question",
            "--no-markup",
            f"--title={title}",
            f"--text={text}",
            f"--ok-label={action}",
            f"--cancel-label={_('Cancel')}",
            "--width=600",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return confirmation.returncode == 0


def desktop_write_floppy(image_path: str | Path) -> int:
    """Collect a drive, confirm destruction, then write and verify a physical floppy."""

    image = Path(image_path).expanduser()
    name = safe_name(image.name)
    dialog = _require_zenity(_("Zenity is required to confirm a physical floppy write safely."))
    drive = _choose_floppy_drive(
        dialog,
        _("Write physical floppy"),
        _("Select the Greaseweazle drive containing the destination floppy."),
    )
    if drive is None:
        return 0
    if not _confirm(
        dialog,
        _("Confirm physical floppy write"),
        _(
            "All existing data on the floppy in drive {drive} will be overwritten.\n\n"
            "Write and verify {image}?"
        ).format(drive=drive, image=name),
        _("Overwrite and verify"),
    ):
        return 0
    try:
        result = _run_with_reported_progress(
            _("Writing physical floppy"),
            _("Preparing {image}…").format(image=name),
            lambda progress: write_floppy(image, drive, progress=progress),
        )
    except (AmigaFSError, OSError) as exc:
        detail = _(
            "{error}\n\nIf writing started, the destination floppy may be incomplete."
        ).format(error=safe_user_message(exc))
        _show_desktop_message(_("Physical floppy write failed"), detail, error=True)
        raise AmigaFSError(detail) from exc
    if result.verified:
        message = _("Greaseweazle wrote and verified {image} in drive {drive}.")
    else:
        message = _(
            "Greaseweazle wrote {image} in drive {drive}. A track-level image cannot be "
            "verified by reading it back."
        )
    _show_desktop_message(
        _("Physical floppy complete"), message.format(image=name, drive=result.drive)
    )
    return 0


def desktop_read_floppy(directory: str | Path) -> int:
    """Capture the floppy in a chosen drive as a new ADF in a folder."""

    destination = Path(directory).expanduser().resolve()
    dialog = _require_zenity(
        _("Run 'amigafs read-floppy DRIVE FILE.adf' to capture a floppy without Zenity.")
    )
    drive = _choose_floppy_drive(
        dialog,
        _("Read physical floppy"),
        _("Select the Greaseweazle drive containing the floppy to read."),
    )
    if drive is None:
        return 0
    separator = "\x1f"
    choice = subprocess.run(
        [
            dialog,
            "--forms",
            f"--title={_('Read physical floppy')}",
            f"--text={_('Save the floppy in drive {drive} as a new image in:').format(drive=drive)}"
            f"\n{destination}",
            f"--add-entry={_('Image filename (default: floppy.adf)')}",
            f"--add-combo={_('Density (default: auto)')}",
            f"--combo-values={'|'.join(DENSITY_CHOICES)}",
            f"--separator={separator}",
            f"--ok-label={_('Read')}",
            f"--cancel-label={_('Cancel')}",
            "--width=600",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if choice.returncode != 0:
        return 0
    values = choice.stdout.rstrip("\n").split(separator)
    if len(values) != 2:
        raise AmigaFSError(_("The image settings dialog returned an invalid response."))
    filename = values[0].strip() or "floppy.adf"
    if Path(filename).name != filename:
        raise AmigaFSError(_("The image name must be a filename, not a path."))
    if not filename.casefold().endswith(".adf"):
        filename = f"{filename}.adf"
    density = values[1].strip() or "auto"
    try:
        result = _run_with_reported_progress(
            _("Reading physical floppy"),
            _("Starting Greaseweazle…"),
            lambda progress: read_floppy(
                destination / filename, drive, density=density, progress=progress
            ),
        )
    except AmigaFSError as exc:
        _show_desktop_message(_("Physical floppy read failed"), str(exc), error=True)
        raise
    _show_desktop_message(
        _("Physical floppy read"),
        _("Saved a complete {format} image as {name}.").format(
            format=result.floppy_format.label, name=result.path.name
        ),
    )
    return 0


def desktop_mount_floppy(*, read_write: bool = False) -> int:
    """Mount the floppy in a chosen Greaseweazle drive."""

    dialog = _require_zenity(
        _("Run 'amigafs mount floppy:A MOUNTPOINT' to mount a floppy without Zenity.")
    )
    drive = _choose_floppy_drive(
        dialog,
        _("Open physical floppy"),
        _("Select the Greaseweazle drive containing the floppy to open."),
    )
    if drive is None:
        return 0
    if read_write and not _confirm(
        dialog,
        _("Open physical floppy read-write"),
        _(
            "The floppy in drive {drive} is read into a working copy. When you unmount "
            "it, the changed tracks are written back to the floppy and verified.\n\n"
            "Leave the floppy in the drive until AmigaFS reports that it was unmounted."
        ).format(drive=drive),
        _("Open read-write"),
    ):
        return 0
    return desktop_mount(f"floppy:{drive}", read_write=read_write)


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _choose_disc(dialog: str, title: str, prompt: str) -> str | None:
    """List eligible removable discs and return the stable path of the chosen one."""

    discs = list_discs()
    if not discs:
        detail = _(
            "No removable or USB disc was found. Attach the Amiga disc or card, make sure "
            "Linux has not mounted it, then try again."
        )
        _show_desktop_message(_("No physical disc detected"), detail, error=True)
        raise AmigaFSError(detail)
    rows: list[str] = []
    for disc in discs:
        rows.extend(
            (
                disc.stable_path,
                disc.model or disc.name,
                _format_size(disc.size),
                disc.name,
            )
        )
    selection = subprocess.run(
        [
            dialog,
            "--list",
            f"--title={title}",
            f"--text={prompt}",
            f"--column={_('Device')}",
            f"--column={_('Disc')}",
            f"--column={_('Capacity')}",
            f"--column={_('Kernel name')}",
            "--hide-column=1",
            "--print-column=1",
            *rows,
            f"--ok-label={_('Continue')}",
            f"--cancel-label={_('Cancel')}",
            "--width=680",
            "--height=320",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if selection.returncode != 0:
        return None
    chosen = selection.stdout.strip().split("|")[0]
    if chosen not in {disc.stable_path for disc in discs}:
        if not chosen:
            return None
        raise AmigaFSError(_("The disc-selection dialog returned an invalid response."))
    return chosen


def desktop_mount_disc(*, read_write: bool = False) -> int:
    """Mount a chosen physical Amiga hard disc, card or other removable disc."""

    dialog = _require_zenity(
        _("Run 'amigafs mount /dev/DISC MOUNTPOINT' to mount a disc without Zenity.")
    )
    device = _choose_disc(
        dialog,
        _("Open physical Amiga disc"),
        _("Select the Amiga disc to open. The computer's own discs are never listed."),
    )
    if device is None:
        return 0
    return desktop_mount(device, read_write=read_write)


def desktop_read_disc(directory: str | Path) -> int:
    """Copy a chosen physical disc to a new hard-disc image in a folder."""

    destination = Path(directory).expanduser().resolve()
    dialog = _require_zenity(
        _("Run 'amigafs read-disc /dev/DISC FILE.hdf' to image a disc without Zenity.")
    )
    device = _choose_disc(
        dialog,
        _("Read physical Amiga disc"),
        _("Select the Amiga disc to copy to a new image."),
    )
    if device is None:
        return 0
    choice = subprocess.run(
        [
            dialog,
            "--entry",
            f"--title={_('Read physical Amiga disc')}",
            f"--text={_('Image filename to create in:')}\n{destination}",
            "--entry-text=disc.hdf",
            f"--ok-label={_('Read')}",
            f"--cancel-label={_('Cancel')}",
            "--width=600",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if choice.returncode != 0:
        return 0
    filename = choice.stdout.strip() or "disc.hdf"
    if Path(filename).name != filename:
        raise AmigaFSError(_("The image name must be a filename, not a path."))
    try:
        result = _run_with_progress_and_cancel(
            _("Reading physical Amiga disc"),
            _("Opening the disc…"),
            lambda progress, cancelled: read_disc(
                device, destination / filename, progress=progress, cancelled=cancelled
            ),
        )
    except OperationCancelled:
        _notify(_("AmigaFS disc read cancelled"), _("No image was created."))
        return 0
    except AmigaFSError as exc:
        _show_desktop_message(_("Physical disc read failed"), str(exc), error=True)
        raise
    _show_desktop_message(
        _("Physical disc read"),
        _("Saved {size} as {name}.").format(size=_format_size(result.size), name=result.image.name),
    )
    return 0


def desktop_write_disc(image_path: str | Path) -> int:
    """Replace a chosen physical disc with a hard-disc image after typed confirmation."""

    image = Path(image_path).expanduser()
    name = safe_name(image.name)
    dialog = _require_zenity(
        _("Run 'amigafs write-disc IMAGE /dev/DISC --confirm NAME' to write without Zenity.")
    )
    device = _choose_disc(
        dialog,
        _("Write image to physical disc"),
        _("Select the disc to overwrite with {image}.").format(image=name),
    )
    if device is None:
        return 0
    kernel_name = Path(os.path.realpath(device)).name
    prompt = _(
        "EVERYTHING on {device} will be replaced by {image}. This cannot be undone.\n\n"
        "Type {name} to confirm:"
    ).format(device=Path(device).name, image=name, name=kernel_name)
    result = subprocess.run(
        [
            dialog,
            "--entry",
            f"--title={_('Confirm physical disc write')}",
            f"--text={prompt}",
            f"--ok-label={_('Overwrite and verify')}",
            f"--cancel-label={_('Cancel')}",
            "--width=620",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0
    confirmation = result.stdout.rstrip("\n")
    try:
        _run_with_reported_progress(
            _("Writing physical disc"),
            _("Opening the disc…"),
            lambda progress: write_disc(
                image, device, confirmation=confirmation, progress=progress
            ),
        )
    except AmigaFSError as exc:
        _show_desktop_message(_("Physical disc write failed"), str(exc), error=True)
        raise
    _show_desktop_message(
        _("Physical disc complete"),
        _("{image} was written to the disc and verified.").format(image=name),
    )
    return 0


def _run_with_progress_and_cancel(
    title: str,
    message: str,
    operation: Callable[[Callable[[int, str], None], Callable[[], bool]], _T],
) -> _T:
    """Run cancellable work with determinate progress."""

    dialog = shutil.which("zenity")
    if dialog is None:
        return operation(lambda _percent, _message: None, lambda: False)
    cancelled = threading.Event()
    try:
        progress = subprocess.Popen(
            [
                dialog,
                "--progress",
                "--auto-close",
                "--percentage=0",
                f"--title={title}",
                f"--text={message}",
                f"--cancel-label={_('Cancel safely')}",
                "--width=560",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return operation(lambda _percent, _message: None, lambda: False)

    def update(percent: int, detail: str) -> None:
        if progress.poll() is not None or progress.stdin is None:
            return
        with suppress(BrokenPipeError, OSError):
            progress.stdin.write(f"#{detail}\n{max(0, min(99, percent))}\n")
            progress.stdin.flush()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="amigafs-transfer") as executor:
        future = executor.submit(operation, update, cancelled.is_set)
        while not future.done():
            if progress.poll() is not None:
                cancelled.set()
                break
            time.sleep(0.05)
        try:
            return future.result()
        finally:
            if progress.poll() is None:
                if progress.stdin is not None:
                    with suppress(BrokenPipeError, OSError):
                        progress.stdin.write("100\n")
                        progress.stdin.close()
                with suppress(subprocess.TimeoutExpired):
                    progress.wait(timeout=2)
                if progress.poll() is None:
                    progress.terminate()


def desktop_create(directory: str | Path, *, kind: str = "floppy") -> int:
    """Collect image settings and create a validated image in a folder."""

    destination = Path(directory).expanduser().resolve()
    hard_disc = kind == "hard-disc"
    dialog = _require_zenity(
        _(
            "Run 'amigafs create-floppy {destination}' or 'amigafs create-hard-disc "
            "{destination}' to create an image without Zenity."
        ).format(destination=destination)
    )
    separator = "\x1f"
    if hard_disc:
        from amigafs.core.create import HARD_DISC_FILESYSTEMS

        arguments = [
            f"--title={_('Create Amiga hard-disc image')}",
            f"--text={_('Create an empty, validated hard-disc image in:')}\n{destination}",
            f"--add-entry={_('Base filename (default: harddisk)')}",
            f"--add-entry={_('Volume name (default: Empty)')}",
            f"--add-entry={_('Capacity (default: 40MB)')}",
            f"--add-entry={_('Partitions (default: 1)')}",
            f"--add-combo={_('Filesystem (default: FFS-INTL)')}",
            f"--combo-values={'|'.join(HARD_DISC_FILESYSTEMS)}",
        ]
        expected = 5
    else:
        from amigafs.core.create import FLOPPY_FILESYSTEMS

        arguments = [
            f"--title={_('Create Amiga floppy image')}",
            f"--text={_('Create an empty, validated ADF in:')}\n{destination}",
            f"--add-entry={_('Base filename (default: blank)')}",
            f"--add-entry={_('Volume name (default: Empty)')}",
            f"--add-combo={_('Density (default: dd)')}",
            "--combo-values=dd|hd",
            f"--add-combo={_('Filesystem (default: OFS)')}",
            f"--combo-values={'|'.join(FLOPPY_FILESYSTEMS)}",
            f"--add-combo={_('Bootable (default: no)')}",
            "--combo-values=no|yes",
        ]
        expected = 5
    choice = subprocess.run(
        [
            dialog,
            "--forms",
            *arguments,
            f"--separator={separator}",
            f"--ok-label={_('Create')}",
            f"--cancel-label={_('Cancel')}",
            "--width=600",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if choice.returncode != 0:
        return 0
    values = [value.strip() for value in choice.stdout.rstrip("\n").split(separator)]
    if len(values) != expected:
        raise AmigaFSError(_("The image settings dialog returned an invalid response."))
    try:
        if hard_disc:
            name, title, capacity, partitions, filesystem = values
            try:
                count = int(partitions) if partitions else 1
            except ValueError as exc:
                raise AmigaFSError(_("The number of partitions must be a whole number.")) from exc
            created = _run_with_reported_progress(
                _("Creating Amiga hard-disc image"),
                _("Checking image settings…"),
                lambda progress: create_hard_disc_image(
                    destination,
                    name=name,
                    title=title,
                    capacity=capacity or "40MB",
                    filesystem=filesystem or "FFS-INTL",
                    partitions=count,
                    progress=progress,
                ),
            )
        else:
            name, title, density, filesystem, bootable = values
            created = _run_with_reported_progress(
                _("Creating Amiga floppy image"),
                _("Checking image settings…"),
                lambda progress: create_floppy_image(
                    destination,
                    name=name,
                    title=title,
                    density=density or "dd",
                    filesystem=filesystem or "OFS",
                    bootable=bootable == "yes",
                    progress=progress,
                ),
            )
    except AmigaFSError as exc:
        _show_desktop_message(_("AmigaFS image creation failed"), str(exc), error=True)
        raise
    _show_desktop_message(
        _("Amiga image created"),
        _("Created and verified {name}.\n\nRight-click the file to open or validate it.").format(
            name=created.path.name
        ),
    )
    return 0


def desktop_configure_mount_location() -> int:
    """Collect and persist the mount location used for future desktop mounts."""

    try:
        current = mount_location()
        displayed = current.mode if current.mode != "custom" else str(current.root)
        preference_note = ""
    except AmigaFSError:
        displayed = "sidebar"
        preference_note = _("\nThe saved preference is invalid; saving will replace it.")
    dialog = _require_zenity(_("Run 'amigafs config-mount-location' to configure mount locations."))
    prompt = (
        _("Enter sidebar, runtime, or an absolute directory path.\n")
        + _("The setting applies to future mounts; existing mounts are unchanged.")
        + preference_note
    )
    choice = subprocess.run(
        [
            dialog,
            "--entry",
            f"--title={_('AmigaFS mount location')}",
            f"--text={prompt}",
            f"--entry-text={displayed}",
            f"--ok-label={_('Save')}",
            f"--cancel-label={_('Cancel')}",
            "--width=620",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if choice.returncode != 0:
        return 0
    try:
        saved = set_mount_location(choice.stdout.rstrip("\n"))
    except AmigaFSError as exc:
        _show_desktop_message(_("AmigaFS mount location was not changed"), str(exc), error=True)
        raise
    effective = mount_location()
    override = (
        _("\n\nAMIGAFS_MOUNT_ROOT still overrides this preference for the current environment.")
        if effective.source == "environment"
        else ""
    )
    _show_desktop_message(
        _("AmigaFS mount location saved"),
        _("Saved mount location:\n{root}\n\nMode: {mode}{override}").format(
            root=saved.root, mode=saved.mode, override=override
        ),
    )
    return 0


def _wait_for_shutdown(target: Path, kind: str | None) -> bool:
    """Wait for a writable daemon, showing progress while it writes changes back."""

    timeout = shutdown_timeout(kind)
    if wait_for_mount_shutdown(target, timeout=2.0):
        return True
    if kind == "physical-floppy":
        message = _("Writing the changed tracks back to the floppy and verifying them…")
    elif kind in {"compressed-image", "flux-image"}:
        message = _("Writing the updated image back…")
    else:
        message = _("Flushing and validating the image…")
    return _run_with_progress(
        _("Unmounting AmigaFS image"),
        message,
        lambda _cancelled: wait_for_mount_shutdown(target, timeout=timeout),
    )


def desktop_unmount(mountpoint: str | Path) -> int:
    target = Path(mountpoint).expanduser().resolve()
    record = mount_at(target)
    if record is None:
        with suppress(OSError):
            target.rmdir()
        _notify(
            _("AmigaFS image unmounted"),
            _("{mountpoint} was already detached.").format(mountpoint=target.name),
        )
        return 0
    if record.read_write is None:
        detail = _(
            "This mount has no lifecycle identity record, so AmigaFS cannot prove its write "
            "mode or final flush state. Unmount it from a terminal before remounting it."
        )
        _notify(_("AmigaFS unmount refused"), detail, error=True)
        raise AmigaFSError(detail)
    command = ["fusermount3", "-u"]
    if record.read_write is False:
        command.append("-z")
    command.append(str(target))
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or _("fusermount3 failed")
        _notify(_("AmigaFS unmount failed"), detail, error=True)
        raise AmigaFSError(
            _("Could not unmount {mountpoint}: {detail}").format(mountpoint=target, detail=detail)
        )
    if record.read_write:
        if not _wait_for_shutdown(target, record.image_kind):
            detail = _(
                "The image detached but its writable daemon has not confirmed a safe flush. "
                "Do not reuse the image or remove the disc until the daemon has exited."
            )
            _show_desktop_message(_("AmigaFS unmount not confirmed"), detail, error=True)
            raise AmigaFSError(detail)
        if record.image_path is not None and pending_recovery(record.image_path) is not None:
            detail = _(
                "The image detached but its changes were not finalised safely; "
                "resolve its recovery checkpoint before mounting it read-write again."
            )
            _show_desktop_message(_("AmigaFS final validation failed"), detail, error=True)
            raise AmigaFSError(detail)
    with suppress(OSError):
        target.rmdir()
    with suppress(OSError):
        target.parent.rmdir()
    if record.read_write:
        if record.image_kind in {"physical-floppy", "physical-disc"}:
            summary = _("{mountpoint} was written and validated. The disc can be removed.")
        else:
            summary = _("{mountpoint} was flushed and validated safely.")
        _notify(_("AmigaFS image unmounted"), summary.format(mountpoint=target.name))
    else:
        _notify(
            _("AmigaFS image detached"),
            _("{mountpoint} was detached read-only.").format(mountpoint=target.name),
        )
    return 0


def cleanup_stale_mountpoint(mountpoint: str | Path) -> bool:
    """Detach a dead FUSE endpoint, leaving healthy mounts untouched."""

    target = Path(mountpoint).expanduser().resolve()
    if not is_mounted(target):
        return False
    try:
        os.listdir(target)
    except OSError as exc:
        if exc.errno not in {errno.ENOTCONN, errno.EIO, errno.ESTALE}:
            raise AmigaFSError(
                _("Could not inspect mounted image {mountpoint}: {error}").format(
                    mountpoint=target, error=exc
                )
            ) from exc
        result = subprocess.run(
            ["fusermount3", "-u", "-z", str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            detail = result.stderr.strip() or _("fusermount3 failed")
            raise AmigaFSError(
                _("Could not clean up stale mount {mountpoint}: {detail}").format(
                    mountpoint=target, detail=detail
                )
            ) from exc
        return True
    return False


def desktop_validate(image_path: str | Path) -> int:
    """Validate an image structure read-only and report the result on the desktop."""

    try:
        report = _run_with_progress(
            _("Validating AmigaFS image"),
            _("Checking partitions, directories and allocation…"),
            lambda cancelled: validate_image_report(image_path, cancelled=cancelled),
        )
    except OperationCancelled:
        _notify(_("AmigaFS validation cancelled"), _("The image was not modified."))
        return 0
    except AmigaFSError as exc:
        _notify(_("AmigaFS validation failed"), str(exc), error=True)
        raise
    source = resolve_image(image_path)
    name = source.name
    if report.fatal_findings or report.warning_findings:
        plan = plan_repairs_from_report(report) if source.capabilities.repair else None
        choice = _show_validation_report(
            name,
            report,
            offer_repair=bool(plan and plan.application_supported),
        )
        if choice is True:
            if plan is None:
                raise AmigaFSError(_("This image format has no supported repair operation."))
            return _confirm_and_apply_repair(source, plan)
        if choice is None:
            first = (*report.fatal_findings, *report.warning_findings)[0]
            remaining = len(report.findings) - 1
            suffix = _(" (+{count} more)").format(count=remaining) if remaining else ""
            _notify(
                _("AmigaFS validation found problems"),
                _("{image}: [{severity}] {code}: {message}{suffix}").format(
                    image=name,
                    severity=first.severity_label,
                    code=first.code,
                    message=first.message,
                    suffix=suffix,
                ),
                error=True,
            )
        return 1
    _notify(
        _("AmigaFS validation passed"),
        _("{image} has no reported filesystem problems.").format(image=name),
    )
    return 0


def _confirm_and_apply_repair(source: ResolvedImage, plan: RepairPlan) -> int:
    """Show the shared typed-confirmation dialog and apply a previously reviewed plan."""

    expected = source.primary_path.name
    dialog = _require_zenity(
        _("Run 'amigafs repair {image} --confirm {name}' to apply the eligible repair.").format(
            image=source.primary_path, name=expected
        )
    )
    actions = "\n".join(f"• {action.title}" for action in plan.actions)
    result = subprocess.run(
        [
            dialog,
            "--entry",
            f"--title={_('Repair AmigaFS image')}",
            f"--text={_('Eligible low-risk repair(s):')}\n{actions}\n\n"
            f"{_('A recovery checkpoint and audit will be created.')}\n"
            f"{_('Type {name} to confirm:').format(name=expected)}",
            f"--ok-label={_('Apply repair')}",
            f"--cancel-label={_('Cancel')}",
            "--width=620",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0
    confirmation = result.stdout.rstrip("\n")
    try:
        repair = _run_with_reported_progress(
            _("Repairing AmigaFS image"),
            _("Preparing the repair…"),
            lambda progress: apply_repairs(
                source.primary_path,
                confirmation=confirmation,
                progress=progress,
            ),
        )
    except AmigaFSError as exc:
        _show_desktop_message(_("AmigaFS repair failed"), str(exc), error=True)
        raise
    _show_desktop_message(
        _("AmigaFS repair completed"),
        _("{image} was repaired and fully verified.\n\nAudit report:\n{audit}").format(
            image=source.name, audit=repair.audit_path
        ),
    )
    return 0


def desktop_repair(image_path: str | Path) -> int:
    """Review an eligible repair and require the exact image filename before applying it."""

    source = resolve_image(image_path)
    if not source.capabilities.repair:
        detail = _("This image format has no supported repair operation.")
        _notify(_("AmigaFS automatic repair refused"), detail, error=True)
        raise AmigaFSError(detail)
    plan = plan_repairs(source.primary_path)
    if plan.clean:
        _notify(
            _("AmigaFS repair"),
            _("{image} needs no repair.").format(image=source.name),
        )
        return 0
    if not plan.application_supported:
        if _show_validation_report(source.name, plan.report) is None:
            _notify(
                _("AmigaFS automatic repair refused"),
                _("This image has no complete low-risk automatic repair plan."),
                error=True,
            )
        return 1
    return _confirm_and_apply_repair(source, plan)


def _recovery_reference(image_path: str | Path) -> str | Path:
    if floppy_reference(image_path) is not None:
        return resolve_image(image_path).primary_path
    return image_path


def _recover_workspace(dialog: str, reference: str | Path) -> int:
    """Offer to salvage or discard an interrupted container or floppy working copy."""

    salvage_choice = _("Save the interrupted working copy as a new image…")
    discard_choice = _("Discard the interrupted changes")
    result = subprocess.run(
        [
            dialog,
            "--list",
            "--radiolist",
            f"--title={_('Resolve interrupted AmigaFS read-write mount')}",
            f"--text={_('The source is unchanged. Choose what to do with the working copy.')}",
            f"--column={_('Selected')}",
            f"--column={_('Action')}",
            "TRUE",
            salvage_choice,
            "FALSE",
            discard_choice,
            f"--ok-label={_('Continue')}",
            f"--cancel-label={_('Cancel')}",
            "--width=620",
            "--height=280",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0
    choice = result.stdout.strip()
    try:
        if choice == salvage_choice:
            saved = subprocess.run(
                [
                    dialog,
                    "--file-selection",
                    "--save",
                    f"--title={_('Save the working copy as')}",
                    "--filename=recovered.adf",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if saved.returncode != 0 or not saved.stdout.strip():
                return 0
            target = salvage_workspace(reference, saved.stdout.strip())
            recover_image(reference, discard=True)
            message = _("The working copy was saved as {name}.").format(name=target.name)
        elif choice == discard_choice:
            message = recover_image(reference, discard=True)
        else:
            return 0
    except AmigaFSError as exc:
        _show_desktop_message(_("AmigaFS recovery failed"), str(exc), error=True)
        raise
    _show_desktop_message(_("AmigaFS recovery complete"), message)
    return 0


def desktop_recover(image_path: str | Path) -> int:
    """Ask the user how to resolve one interrupted writable session."""

    dialog = _require_zenity(
        _("Recovery needs a choice. Run 'amigafs recover IMAGE --restore' or '--discard'.")
    )
    reference = _recovery_reference(image_path)
    info = pending_recovery(reference)
    if info is None:
        _notify(_("AmigaFS recovery"), _("No recovery checkpoint is pending."))
        return 0
    if info.kind == KIND_WORKSPACE:
        return _recover_workspace(dialog, reference)
    restore_choice = _("Restore image to the pre-mount checkpoint")
    discard_choice = _("Keep the current image and discard the checkpoint")
    result = subprocess.run(
        [
            dialog,
            "--list",
            "--radiolist",
            f"--title={_('Resolve interrupted AmigaFS read-write mount')}",
            f"--text={_('Choose how to resolve the retained pre-mount checkpoint.')}",
            f"--column={_('Selected')}",
            f"--column={_('Action')}",
            "TRUE",
            restore_choice,
            "FALSE",
            discard_choice,
            f"--ok-label={_('Continue')}",
            f"--cancel-label={_('Cancel')}",
            "--width=620",
            "--height=280",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0
    choice = result.stdout.strip()
    try:
        if choice == restore_choice:
            message = _run_with_progress(
                _("Restoring AmigaFS image"),
                _("Restoring every block the interrupted session changed…"),
                lambda cancelled: recover_image(reference, restore=True, cancelled=cancelled),
            )
        elif choice == discard_choice:
            message = recover_image(reference, discard=True)
        else:
            return 0
    except OperationCancelled:
        _show_desktop_message(
            _("AmigaFS recovery cancelled"),
            _(
                "Recovery stopped before anything was restored. The image was not changed and "
                "the checkpoint is still available."
            ),
        )
        return 0
    except AmigaFSError as exc:
        _show_desktop_message(_("AmigaFS recovery failed"), str(exc), error=True)
        raise
    _show_desktop_message(_("AmigaFS recovery complete"), message)
    return 0


def notify_mount_failure(message: str) -> None:
    """Surface a detached mount or final-validation failure to the desktop."""

    _notify(_("AmigaFS mount failed"), message, error=True)


def notify_write_back(name: str) -> None:
    """Tell the desktop that a detached daemon is writing changes to its source."""

    _notify(
        _("AmigaFS is writing changes back"),
        _("Do not remove or reuse {image} until AmigaFS reports that it is finished.").format(
            image=safe_name(name)
        ),
    )


__all__ = [
    "background_mount",
    "cleanup_stale_mountpoint",
    "desktop_configure_mount_location",
    "desktop_create",
    "desktop_mount",
    "desktop_mount_disc",
    "desktop_mount_floppy",
    "desktop_open",
    "desktop_open_file_forge",
    "desktop_read_disc",
    "desktop_read_floppy",
    "desktop_recover",
    "desktop_repair",
    "desktop_unmount",
    "desktop_validate",
    "desktop_write_disc",
    "desktop_write_floppy",
    "local_image_reference",
    "mount_root",
    "mountpoint_for_image",
    "notify_mount_failure",
    "notify_write_back",
    "runtime_root",
    "shutdown_timeout",
]
