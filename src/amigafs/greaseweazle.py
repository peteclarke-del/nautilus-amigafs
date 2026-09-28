"""Optional, shell-free Greaseweazle physical-floppy integration.

Three operations use the drive:

* writing an image to a physical floppy, with verification;
* reading a physical floppy into a new image;
* mounting a physical floppy, which reads it into a private working copy and,
  after a clean read-write unmount, writes back only the cylinders that changed.

An AmigaDOS sector image is always written with an explicit Amiga disk format,
so a same-named image from another system is never guessed at. Track-level
images are passed to Greaseweazle unchanged, because they may hold data a
sector image cannot represent.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from amigafs.core.containers import (
    FLOPPY_FORMATS,
    FloppyFormat,
    Workspace,
    container_kind,
    floppy_format_for_size,
    max_workspace_bytes,
    read_magic,
    sectors_complete,
    tool_environment,
)
from amigafs.errors import AmigaFSError
from amigafs.i18n import _
from amigafs.privacy import safe_user_message

SUPPORTED_SUFFIXES = frozenset({".adf", ".adz", ".dms", ".hfe", ".scp", ".ipf"})
NATIVE_CONTAINERS = frozenset({"hfe", "scp", "ipf"})
DRIVE_CHOICES = ("A", "B", "0", "1", "2", "3")
DENSITY_CHOICES = ("auto", "dd", "hd")
INFO_TIMEOUT = 4.0
DRIVE_PROBE_TIMEOUT = 5.0
WRITE_TIMEOUT = 30 * 60.0
READ_TIMEOUT = 30 * 60.0
SERIAL_DEVICE_DIRECTORY = Path("/dev/serial/by-id")
_TRACK = re.compile(r"^T(?P<cylinder>\d+)\.(?P<head>\d+):")
_GEOMETRY = re.compile(
    r"(?:Writing|Reading) c=(?P<first>\d+)-(?P<last>\d+):h=(?P<head_first>\d+)"
    r"(?:-(?P<head_last>\d+))?"
)
_RPM = re.compile(r"\bRate:\s*\d+(?:\.\d+)?\s*rpm\b", re.IGNORECASE)

ProgressCallback = Callable[[int, str], None]


@dataclass(frozen=True)
class FloppyWriteResult:
    """Outcome reported by a successful Greaseweazle write."""

    drive: str
    verified: bool
    tracks: int = 0


@dataclass(frozen=True)
class FloppyReadResult:
    """Outcome of capturing a physical floppy as a sector image."""

    drive: str
    path: Path
    floppy_format: FloppyFormat


def supports_physical_write(path: str | Path) -> bool:
    """Return whether the filename is one AmigaFS offers to write to a floppy."""

    return Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def _command_responds(command: str) -> bool:
    """Probe one resolved command without exposing subprocess details to callers."""

    try:
        probe = subprocess.run(
            [command, "info"],
            check=False,
            capture_output=True,
            text=True,
            env=tool_environment(),
            timeout=INFO_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = f"{probe.stdout}\n{probe.stderr}"
    return probe.returncode == 0 and "not found" not in output.casefold()


def responsive_command() -> str | None:
    """Return a usable gw command only when a device responds to ``gw info``."""

    command = shutil.which("gw")
    if command is None:
        return None
    return command if _command_responds(command) else None


def detected_command(path: str | Path) -> str | None:
    """Return a usable gw command for one image the drive could be written from."""

    if not supports_physical_write(path):
        return None
    return responsive_command()


def device_available() -> bool:
    """Return whether udev exposes an accessible Greaseweazle serial device."""

    try:
        devices = tuple(SERIAL_DEVICE_DIRECTORY.iterdir())
    except OSError:
        return False
    for device in devices:
        if "greaseweazle" not in device.name.casefold():
            continue
        try:
            target = device.resolve(strict=True)
        except OSError:
            continue
        if os.access(target, os.R_OK | os.W_OK):
            return True
    return False


def floppy_drive_available() -> bool:
    """Return immediate executable and udev availability for a desktop menu."""

    return shutil.which("gw") is not None and device_available()


def _plausible_image(path: Path) -> bool:
    kind = container_kind(path)
    suffix = path.suffix.casefold()
    if kind in NATIVE_CONTAINERS or kind in {"gzip", "dms"}:
        return True
    if suffix != ".adf":
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    return floppy_format_for_size(size) is not None and read_magic(path, 3) == b"DOS"


def physical_write_available(path: str | Path) -> bool:
    """Return whether the menu should offer to write this image to a floppy."""

    selected = Path(path)
    if not supports_physical_write(selected) or not _plausible_image(selected):
        return False
    return floppy_drive_available()


def _reset_after_probe_timeout(command: str) -> None:
    """Best-effort controller reset to deselect drives and stop their motors."""

    with suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            [command, "reset"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=tool_environment(),
            timeout=INFO_TIMEOUT,
        )


def _drive_has_index(command: str, drive: str) -> bool:
    """Probe one drive for an indexed disk without modifying its contents."""

    try:
        probe = subprocess.run(
            [command, "rpm", f"--drive={drive}", "--nr=1"],
            check=False,
            capture_output=True,
            text=True,
            env=tool_environment(),
            timeout=DRIVE_PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _reset_after_probe_timeout(command)
        return False
    except OSError:
        return False
    output = f"{probe.stdout}\n{probe.stderr}"
    return probe.returncode == 0 and _RPM.search(output) is not None


def detected_drives(
    *,
    command: str | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[str, ...]:
    """Return drives that report index pulses from an inserted floppy.

    PC and Shugart identifiers describe alternative bus configurations. Probe
    the Shugart group only when no PC-bus drive responds, avoiding duplicate or
    misleading choices for normal single-bus installations.
    """

    executable = command or responsive_command()
    if executable is None:
        return ()
    report = progress or (lambda _percent, _detail: None)
    groups = (("A", "B"), ("0", "1", "2", "3"))
    completed = 0
    for drives in groups:
        found: list[str] = []
        for drive in drives:
            report(
                5 + completed * 90 // len(DRIVE_CHOICES),
                _("Checking physical drive {drive}…").format(drive=drive),
            )
            if _drive_has_index(executable, drive):
                found.append(drive)
            completed += 1
        if found:
            report(100, _("Physical drives detected."))
            return tuple(found)
    report(100, _("No physical drive with an indexed floppy was detected."))
    return ()


def normalise_drive(drive: str) -> str:
    selected = drive.strip()
    selected = selected.upper() if selected.lower() in {"a", "b"} else selected
    if selected not in DRIVE_CHOICES:
        raise AmigaFSError(_("The selected Greaseweazle drive is invalid."))
    return selected


def _track_total(line: str) -> int | None:
    match = _GEOMETRY.search(line)
    if match is None:
        return None
    cylinders = int(match["last"]) - int(match["first"]) + 1
    last_head = int(match["head_last"] or match["head_first"])
    heads = last_head - int(match["head_first"]) + 1
    return cylinders * heads


@dataclass
class _StreamResult:
    returncode: int
    recent: tuple[str, ...]
    output: str
    tracks: int
    verified: bool
    timed_out: bool


def _stream(
    arguments: list[str],
    *,
    timeout: float,
    report: ProgressCallback,
    activity: str,
    first_percent: int = 5,
    last_percent: int = 95,
    expected_tracks: int | None = None,
) -> _StreamResult:
    """Run one gw command, following its per-track output as progress."""

    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=tool_environment(),
        )
    except OSError as exc:
        raise AmigaFSError(
            _("Could not start Greaseweazle: {detail}").format(detail=safe_user_message(exc))
        ) from exc

    timed_out = threading.Event()

    def stop() -> None:
        timed_out.set()
        with suppress(OSError):
            process.terminate()

    watchdog = threading.Timer(timeout, stop)
    watchdog.daemon = True
    watchdog.start()
    recent: deque[str] = deque(maxlen=8)
    lines: list[str] = []
    tracks: set[tuple[int, int]] = set()
    total = expected_tracks
    verified = False
    assert process.stdout is not None
    try:
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            recent.append(line)
            lines.append(line)
            total = _track_total(line) or total
            track = _TRACK.match(line)
            if track is not None:
                tracks.add((int(track["cylinder"]), int(track["head"])))
                span = last_percent - first_percent
                percent = (
                    first_percent + min(span, int(span * len(tracks) / total))
                    if total
                    else first_percent
                )
                report(
                    percent,
                    activity.format(track=f"{track['cylinder']}.{track['head']}"),
                )
            if "Verify Failure" in line:
                report(first_percent, _("Retrying track verification…"))
            if "All tracks verified" in line:
                verified = True
        returncode = process.wait()
    except BaseException:
        with suppress(OSError):
            process.terminate()
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=5)
        raise
    finally:
        watchdog.cancel()
    return _StreamResult(
        returncode=returncode,
        recent=tuple(recent),
        output="\n".join(lines),
        tracks=len(tracks),
        verified=verified,
        timed_out=timed_out.is_set(),
    )


def _snapshot(image: Path, destination: Path) -> None:
    before = image.stat()
    if not image.is_file():
        raise AmigaFSError(_("The selected floppy image is not a regular file."))
    with image.open("rb") as source, destination.open("xb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    after = image.stat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or destination.stat().st_size != after.st_size:
        raise AmigaFSError(
            _("The floppy image changed while it was being prepared; no physical write started.")
        )


def _sector_image(snapshot: Path, directory: Path, kind: str | None) -> Path:
    """Turn a stable snapshot into the plain sector image Greaseweazle is given."""

    if kind is None:
        return snapshot
    decoded = directory / "decoded.adf"
    if kind == "gzip":
        try:
            with gzip.open(snapshot, "rb") as packed, decoded.open("xb") as target:
                written = 0
                while chunk := packed.read(1024 * 1024):
                    written += len(chunk)
                    if written > max_workspace_bytes():
                        raise AmigaFSError(_("The compressed image is too large for a floppy."))
                    target.write(chunk)
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise AmigaFSError(
                _("The compressed image could not be unpacked: {error}").format(error=exc)
            ) from exc
        return decoded
    if kind == "dms":
        from amigafs.core.containers import _decode_dms

        _decode_dms(snapshot, decoded)
        return decoded
    raise AmigaFSError(_("This image format cannot be written to a physical floppy."))


def greaseweazle_format(path: str | Path) -> str:
    """Resolve one plain sector image to an explicit Amiga disk format."""

    try:
        size = Path(path).stat().st_size
    except OSError as exc:
        raise AmigaFSError(
            _("Could not open the floppy image: {detail}").format(detail=safe_user_message(exc))
        ) from exc
    floppy_format = floppy_format_for_size(size)
    if floppy_format is None:
        raise AmigaFSError(
            _(
                "The image is {size} bytes, which is neither an 880 KiB nor a 1760 KiB Amiga "
                "floppy."
            ).format(size=size)
        )
    return floppy_format.greaseweazle_name


def _require_success(result: _StreamResult, *, writing: bool) -> None:
    trailer = (
        _(" The physical floppy may be incomplete; do not rely on its contents.") if writing else ""
    )
    if result.timed_out:
        raise AmigaFSError(_("Greaseweazle exceeded its time limit.") + trailer)
    if result.returncode != 0:
        detail = safe_user_message(result.recent[-1] if result.recent else _("unknown error"))
        raise AmigaFSError(
            _("Greaseweazle could not complete the operation: {detail}.").format(detail=detail)
            + trailer
        )


def write_floppy(
    image_path: str | Path,
    drive: str,
    *,
    progress: ProgressCallback | None = None,
    cylinders: tuple[int, ...] | None = None,
) -> FloppyWriteResult:
    """Write one stable image snapshot and retain Greaseweazle verification defaults."""

    report = progress or (lambda _percent, _detail: None)
    selected_drive = normalise_drive(drive)
    try:
        image = Path(image_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise AmigaFSError(
            _("Could not open the floppy image: {detail}").format(detail=safe_user_message(exc))
        ) from exc
    if not supports_physical_write(image):
        raise AmigaFSError(_("AmigaFS does not write this kind of file to a floppy."))
    kind = container_kind(image)
    native = kind in NATIVE_CONTAINERS
    if kind is not None and not native and kind not in {"gzip", "dms"}:
        raise AmigaFSError(_("This image format cannot be written to a physical floppy."))
    command = responsive_command()
    if command is None:
        raise AmigaFSError(
            _("No responsive Greaseweazle device was detected; reconnect it and try again.")
        )

    report(1, _("Preparing a stable image snapshot…"))
    with tempfile.TemporaryDirectory(prefix="amigafs-gw-") as temporary:
        directory = Path(temporary)
        suffix = image.suffix.casefold() if native else ".bin"
        snapshot = directory / f"image{suffix}"
        try:
            _snapshot(image, snapshot)
            if native:
                source = snapshot
                format_name = None
            else:
                source = _sector_image(snapshot, directory, kind)
                if source.suffix != ".adf":
                    renamed = directory / "image.adf"
                    os.replace(source, renamed)
                    source = renamed
                format_name = greaseweazle_format(source)
        except OSError as exc:
            raise AmigaFSError(
                _("Could not prepare the floppy image: {detail}").format(
                    detail=safe_user_message(exc)
                )
            ) from exc
        report(5, _("Starting Greaseweazle…"))
        arguments = [command, "write", f"--drive={selected_drive}"]
        if format_name is not None:
            arguments.append(f"--format={format_name}")
        if cylinders is not None:
            if not cylinders:
                raise AmigaFSError(_("No cylinder was selected for writing."))
            arguments.append("--tracks=c=" + ",".join(str(item) for item in sorted(cylinders)))
        arguments.append(str(source))
        result = _stream(
            arguments,
            timeout=WRITE_TIMEOUT,
            report=report,
            activity=_("Writing and verifying track {track}"),
            expected_tracks=None if cylinders is None else len(cylinders) * 2,
        )
        _require_success(result, writing=True)
        if not result.verified and not native:
            raise AmigaFSError(
                _(
                    "Greaseweazle completed the write but did not confirm verification. "
                    "The physical floppy may be incomplete; do not rely on its contents."
                )
            )
        report(100, _("All tracks written and verified."))
        return FloppyWriteResult(
            drive=selected_drive, verified=result.verified, tracks=result.tracks
        )


def _candidate_formats(density: str) -> tuple[FloppyFormat, ...]:
    selected = density.strip().casefold() or "auto"
    if selected not in DENSITY_CHOICES:
        raise AmigaFSError(_("Choose a floppy density of auto, dd or hd."))
    double = next(item for item in FLOPPY_FORMATS if item.sectors_per_track == 11)
    high = next(item for item in FLOPPY_FORMATS if item.sectors_per_track == 22)
    if selected == "dd":
        return (double,)
    if selected == "hd":
        return (high,)
    # Nearly every Amiga floppy is double density, so it is tried first.
    return (double, high)


def capture_floppy(
    destination: Path,
    drive: str,
    *,
    density: str = "auto",
    progress: ProgressCallback | None = None,
    command: str | None = None,
) -> FloppyReadResult:
    """Read the floppy in a drive into ``destination`` as a complete sector image.

    An incomplete read is never kept: a floppy with unreadable or non-AmigaDOS
    tracks cannot be represented by a sector image.
    """

    report = progress or (lambda _percent, _detail: None)
    selected_drive = normalise_drive(drive)
    executable = command or responsive_command()
    if executable is None:
        raise AmigaFSError(
            _("No responsive Greaseweazle device was detected; reconnect it and try again.")
        )
    failures: list[str] = []
    candidates = _candidate_formats(density)
    for index, floppy_format in enumerate(candidates):
        destination.unlink(missing_ok=True)
        base = index * 90 // len(candidates)
        report(base + 2, _("Reading the floppy as {format}…").format(format=floppy_format.label))
        result = _stream(
            [
                executable,
                "read",
                f"--drive={selected_drive}",
                f"--format={floppy_format.greaseweazle_name}",
                str(destination),
            ],
            timeout=READ_TIMEOUT,
            report=report,
            activity=_("Reading track {track}"),
            first_percent=base + 5,
            last_percent=base + 90 // len(candidates),
            expected_tracks=floppy_format.cylinders * 2,
        )
        try:
            _require_success(result, writing=False)
        except AmigaFSError as exc:
            failures.append(str(exc))
            destination.unlink(missing_ok=True)
            continue
        if (
            sectors_complete(result.output)
            and destination.is_file()
            and destination.stat().st_size == floppy_format.size
        ):
            os.chmod(destination, 0o600)
            report(100, _("Floppy read completely."))
            return FloppyReadResult(
                drive=selected_drive, path=destination, floppy_format=floppy_format
            )
        failures.append(
            _("not every sector could be read as {format}").format(format=floppy_format.label)
        )
        destination.unlink(missing_ok=True)
    raise AmigaFSError(
        _(
            "The floppy could not be read as a complete AmigaDOS disk: {detail}. A damaged or "
            "copy-protected disk can still be captured as a track-level image with 'gw read'."
        ).format(detail=failures[-1] if failures else _("unknown error"))
    )


def read_floppy(
    destination: str | Path,
    drive: str,
    *,
    density: str = "auto",
    progress: ProgressCallback | None = None,
) -> FloppyReadResult:
    """Capture a physical floppy as a new ADF, never overwriting an existing file."""

    requested = Path(destination).expanduser()
    parent = requested.parent.resolve()
    target = parent / requested.name
    if target.suffix.casefold() != ".adf":
        raise AmigaFSError(_("A floppy is captured as an .adf image."))
    if not parent.is_dir():
        raise AmigaFSError(
            _("The destination directory does not exist: {path}").format(path=parent)
        )
    if any(child.name.casefold() == target.name.casefold() for child in parent.iterdir()):
        raise AmigaFSError(_("Refusing to overwrite an existing file: {path}").format(path=target))
    with tempfile.TemporaryDirectory(prefix="amigafs-gw-") as temporary:
        captured = capture_floppy(
            Path(temporary) / "capture.adf", drive, density=density, progress=progress
        )
        staged = parent / f".{target.name}.amigafs-{os.getpid()}"
        try:
            shutil.copyfile(captured.path, staged)
            with staged.open("rb") as handle:
                os.fsync(handle.fileno())
            os.chmod(staged, 0o644)
            os.link(staged, target)
        except OSError as exc:
            raise AmigaFSError(
                _("Could not save the captured floppy: {error}").format(error=exc)
            ) from exc
        finally:
            staged.unlink(missing_ok=True)
    return FloppyReadResult(drive=captured.drive, path=target, floppy_format=captured.floppy_format)


def changed_cylinders(
    original: Path, current: Path, floppy_format: FloppyFormat
) -> tuple[int, ...]:
    """Return the cylinders whose sectors differ between two images of one disk."""

    cylinder_bytes = 2 * floppy_format.sectors_per_track * 512
    changed = []
    with original.open("rb") as before, current.open("rb") as after:
        for cylinder in range(floppy_format.cylinders):
            if before.read(cylinder_bytes) != after.read(cylinder_bytes):
                changed.append(cylinder)
    return tuple(changed)


class FloppyWorkspace(Workspace):
    """The working copy of a mounted physical floppy."""

    kind = "greaseweazle"
    writable_back = True

    def __init__(
        self,
        source: Path,
        raw_path: Path,
        original_path: Path,
        drive: str,
        floppy_format: FloppyFormat,
        *,
        temporary: tempfile.TemporaryDirectory[str] | None = None,
    ) -> None:
        super().__init__(source, raw_path, None, temporary=temporary)
        self.original_path = original_path
        self.drive = drive
        self.floppy_format = floppy_format

    def export(self, *, progress: ProgressCallback | None = None) -> None:
        cylinders = changed_cylinders(self.original_path, self.raw_path, self.floppy_format)
        if not cylinders:
            return
        staged = self.raw_path.with_name("write-back.adf")
        staged.unlink(missing_ok=True)
        try:
            shutil.copyfile(self.raw_path, staged)
            write_floppy(staged, self.drive, progress=progress, cylinders=cylinders)
        except AmigaFSError as exc:
            raise AmigaFSError(
                _(
                    "{error} The changes are kept in a working copy; save it with "
                    "'amigafs recover floppy:{drive} --salvage FILE.adf'."
                ).format(error=exc, drive=self.drive)
            ) from exc
        finally:
            staged.unlink(missing_ok=True)
        shutil.copyfile(self.raw_path, self.original_path)


def read_floppy_workspace(
    source: Path,
    drive: str,
    *,
    directory: Path | None = None,
    progress: ProgressCallback | None = None,
    density: str = "auto",
) -> FloppyWorkspace:
    """Read the floppy in a drive into a private working copy for mounting."""

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if directory is None:
        temporary = tempfile.TemporaryDirectory(prefix="amigafs-floppy-")
        root = Path(temporary.name)
    else:
        root = directory
    raw = root / "workspace.img"
    original = root / "source.adf"
    try:
        raw.unlink(missing_ok=True)
        original.unlink(missing_ok=True)
        captured = capture_floppy(original, drive, density=density, progress=progress)
        shutil.copyfile(original, raw)
        os.chmod(raw, 0o600)
        return FloppyWorkspace(
            source, raw, original, captured.drive, captured.floppy_format, temporary=temporary
        )
    except BaseException:
        raw.unlink(missing_ok=True)
        original.unlink(missing_ok=True)
        if temporary is not None:
            temporary.cleanup()
        raise


__all__ = [
    "DENSITY_CHOICES",
    "DRIVE_CHOICES",
    "DRIVE_PROBE_TIMEOUT",
    "FloppyReadResult",
    "FloppyWorkspace",
    "FloppyWriteResult",
    "SUPPORTED_SUFFIXES",
    "WRITE_TIMEOUT",
    "capture_floppy",
    "changed_cylinders",
    "detected_command",
    "detected_drives",
    "device_available",
    "floppy_drive_available",
    "greaseweazle_format",
    "normalise_drive",
    "physical_write_available",
    "read_floppy",
    "read_floppy_workspace",
    "responsive_command",
    "supports_physical_write",
    "write_floppy",
]
