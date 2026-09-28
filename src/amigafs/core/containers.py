"""Decoded working copies of images that are not plain sector dumps.

A compressed image, a DiskMasher archive, an extended ADF, a track-level image
or a physical floppy cannot be edited in place. Each is decoded into a private
raw sector image, which is what the filesystem drivers open. A format that can
be re-encoded without losing information is written back when a writable
session closes cleanly; every other format is read-only.

Nothing here decides what filesystem an image holds. That is left to content
detection on the decoded sectors.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import stat
import struct
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from amigafs.errors import AmigaFSError, UnsupportedImageError
from amigafs.i18n import _
from amigafs.privacy import safe_user_message

SECTOR_BYTES = 512
DD_TRACK_BYTES = 11 * SECTOR_BYTES
HD_TRACK_BYTES = 22 * SECTOR_BYTES
DD_IMAGE_BYTES = 80 * 2 * DD_TRACK_BYTES
HD_IMAGE_BYTES = 80 * 2 * HD_TRACK_BYTES

GZIP_MAGIC = b"\x1f\x8b"
DMS_MAGIC = b"DMS!"
EXTENDED_ADF_V1 = b"UAE--ADF"
EXTENDED_ADF_V2 = b"UAE-1ADF"
HFE_V1_SIGNATURE = b"HXCPICFE"
HFE_V3_SIGNATURE = b"HXCHFEV3"
SCP_MAGIC = b"SCP"
IPF_MAGIC = b"CAPS"

CONVERSION_TIMEOUT = 5 * 60.0
FLOPPY_TIMEOUT = 30 * 60.0
MAX_FLUX_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_WORKSPACE_BYTES = 8 * 1024 * 1024 * 1024
_COPY_BYTES = 1024 * 1024
_SECTOR_RESULT = re.compile(
    r"Found\s+(?P<found>\d+)\s+sectors\s+of\s+(?P<total>\d+)\s+\((?P<percent>\d+)%\)"
)

ProgressCallback = Callable[[int, str], None]
SourceSignature = tuple[int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class FloppyFormat:
    """One AmigaDOS sector layout Greaseweazle can decode and encode."""

    greaseweazle_name: str
    size: int
    cylinders: int
    sectors_per_track: int
    label: str


# The high-density layout comes first: Greaseweazle can otherwise decode a
# plausible half of a high-density disk with the double-density definition.
FLOPPY_FORMATS = (
    FloppyFormat("amiga.amigados_hd", HD_IMAGE_BYTES, 80, 22, "Amiga HD, 1760 KiB"),
    FloppyFormat("amiga.amigados", DD_IMAGE_BYTES, 80, 11, "Amiga DD, 880 KiB"),
)


def floppy_format_for_size(size: int) -> FloppyFormat | None:
    return next((item for item in FLOPPY_FORMATS if item.size == size), None)


def max_workspace_bytes() -> int:
    configured = os.environ.get("AMIGAFS_MAX_WORKSPACE_BYTES")
    if configured:
        try:
            value = int(configured)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return DEFAULT_MAX_WORKSPACE_BYTES


def tool_environment() -> dict[str, str]:
    return {
        name: value
        for name in ("HOME", "LANG", "LC_ALL", "PATH")
        if (value := os.environ.get(name)) is not None
    }


def read_magic(path: str | Path, length: int = 16) -> bytes:
    try:
        with Path(path).open("rb") as handle:
            return handle.read(length)
    except OSError:
        return b""


def container_kind(path: str | Path) -> str | None:
    """Identify a container from its header, never from its name."""

    magic = read_magic(path)
    if magic.startswith(GZIP_MAGIC):
        return "gzip"
    if magic.startswith(DMS_MAGIC):
        return "dms"
    if magic.startswith((EXTENDED_ADF_V1, EXTENDED_ADF_V2)):
        return "extended-adf"
    if magic.startswith((HFE_V1_SIGNATURE, HFE_V3_SIGNATURE)):
        return "hfe"
    if magic.startswith(SCP_MAGIC):
        return "scp"
    if magic.startswith(IPF_MAGIC):
        return "ipf"
    return None


def hfe_version(path: str | Path) -> int | None:
    magic = read_magic(path, 8)
    if magic == HFE_V1_SIGNATURE:
        return 1
    if magic == HFE_V3_SIGNATURE:
        return 3
    return None


def source_signature(descriptor: int, path: Path) -> SourceSignature:
    opened = os.fstat(descriptor)
    current = path.stat(follow_symlinks=False)
    return (
        current.st_dev,
        current.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    )


def replace_preserving_identity(encoded: Path, destination: Path) -> None:
    """Atomically publish a re-encoded container over its source."""

    before = destination.stat(follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise AmigaFSError(_("The image destination is no longer a regular file."))
    if not encoded.is_file() or encoded.stat().st_size == 0:
        raise AmigaFSError(_("No replacement image was produced."))
    os.chmod(encoded, stat.S_IMODE(before.st_mode))
    encoded_stat = encoded.stat(follow_symlinks=False)
    if (encoded_stat.st_uid, encoded_stat.st_gid) != (before.st_uid, before.st_gid):
        try:
            os.chown(encoded, before.st_uid, before.st_gid)
        except PermissionError as exc:
            raise AmigaFSError(
                _("The replacement image could not retain the source file ownership.")
            ) from exc
    try:
        for name in os.listxattr(destination, follow_symlinks=False):
            value = os.getxattr(destination, name, follow_symlinks=False)
            os.setxattr(encoded, name, value, follow_symlinks=False)
    except OSError as exc:
        raise AmigaFSError(
            _("The replacement image could not retain the source file attributes.")
        ) from exc
    with encoded.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(encoded, destination)
    directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _sibling(destination: Path, suffix: str) -> Path:
    return destination.parent / f".{destination.stem}.amigafs-{os.getpid()}{suffix}"


def run_greaseweazle(
    arguments: list[str], *, timeout: float = CONVERSION_TIMEOUT, command: str | None = None
) -> str:
    """Run one ``gw`` command without a shell and return its combined output."""

    executable = command or shutil.which("gw")
    if executable is None:
        raise UnsupportedImageError(
            _("This operation requires the Greaseweazle host tools ('gw').")
        )
    try:
        result = subprocess.run(
            [executable, *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=tool_environment(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AmigaFSError(_("Greaseweazle did not finish in time.")) from exc
    except OSError as exc:
        raise AmigaFSError(
            _("Could not start Greaseweazle: {error}").format(error=safe_user_message(exc))
        ) from exc
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        detail = safe_user_message(lines[-1]) if lines else _("unknown error")
        raise AmigaFSError(_("Greaseweazle failed: {detail}").format(detail=detail))
    return output


def sectors_complete(output: str) -> bool:
    matches = tuple(_SECTOR_RESULT.finditer(output))
    return bool(matches) and matches[-1]["percent"] == "100"


class Workspace:
    """A private decoded sector image and the means to write it back."""

    kind = "raw"
    writable_back = False

    def __init__(
        self,
        source: Path,
        raw_path: Path,
        signature: SourceSignature | None,
        *,
        temporary: tempfile.TemporaryDirectory[str] | None = None,
    ) -> None:
        self.source = source
        self.raw_path = raw_path
        self.source_signature = signature
        self._temporary = temporary
        self._closed = False

    @property
    def description(self) -> str:
        return self.kind

    def export(self, *, progress: ProgressCallback | None = None) -> None:
        raise AmigaFSError(_("This image format cannot be written back."))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._temporary is not None:
            self._temporary.cleanup()

    def __del__(self) -> None:
        with suppress(Exception):
            self.close()


class GzipWorkspace(Workspace):
    """An ADZ, HDZ or other gzip-wrapped sector image."""

    kind = "gzip"
    writable_back = True

    def export(self, *, progress: ProgressCallback | None = None) -> None:
        encoded = _sibling(self.source, self.source.suffix or ".gz")
        encoded.unlink(missing_ok=True)
        try:
            total = max(1, self.raw_path.stat().st_size)
            done = 0
            with (
                self.raw_path.open("rb") as raw,
                encoded.open("xb") as target,
                gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as packed,
            ):
                while chunk := raw.read(_COPY_BYTES):
                    packed.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done * 100 // total, _("Compressing the image…"))
            replace_preserving_identity(encoded, self.source)
        finally:
            encoded.unlink(missing_ok=True)


class ArchiveWorkspace(Workspace):
    """A read-only decode of a DiskMasher archive or extended ADF."""

    def __init__(self, *args: object, kind: str, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.kind = kind


class FluxWorkspace(Workspace):
    """A track-level image decoded to AmigaDOS sectors by Greaseweazle."""

    def __init__(
        self,
        source: Path,
        raw_path: Path,
        signature: SourceSignature | None,
        *,
        kind: str,
        floppy_format: FloppyFormat,
        version: int | None,
        temporary: tempfile.TemporaryDirectory[str] | None = None,
    ) -> None:
        super().__init__(source, raw_path, signature, temporary=temporary)
        self.kind = kind
        self.floppy_format = floppy_format
        self.version = version
        self.writable_back = kind == "hfe"

    def export(self, *, progress: ProgressCallback | None = None) -> None:
        if not self.writable_back:
            raise AmigaFSError(_("This image format cannot be written back."))
        encoded = _sibling(self.source, ".hfe")
        encoded.unlink(missing_ok=True)
        output = f"{encoded}::version=3" if self.version == 3 else str(encoded)
        if progress is not None:
            progress(10, _("Encoding the HFE image…"))
        try:
            run_greaseweazle(
                [
                    "convert",
                    f"--format={self.floppy_format.greaseweazle_name}",
                    str(self.raw_path),
                    output,
                ]
            )
            replace_preserving_identity(encoded, self.source)
        finally:
            encoded.unlink(missing_ok=True)


class _Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


def _bounded_copy(source: _Readable, target: Path, limit: int) -> int:
    written = 0
    with target.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        while chunk := source.read(_COPY_BYTES):
            written += len(chunk)
            if written > limit:
                raise UnsupportedImageError(
                    _("The compressed image expands beyond the {limit} byte safety limit.").format(
                        limit=limit
                    )
                )
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    return written


def _decode_gzip(stable: Path, raw: Path) -> None:
    try:
        with gzip.open(stable, "rb") as packed:
            size = _bounded_copy(packed, raw, max_workspace_bytes())
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raw.unlink(missing_ok=True)
        raise UnsupportedImageError(
            _("The compressed image could not be unpacked: {error}").format(error=exc)
        ) from exc
    if size == 0 or size % SECTOR_BYTES:
        raw.unlink(missing_ok=True)
        raise UnsupportedImageError(
            _("The compressed image does not contain a whole number of 512-byte sectors.")
        )


def _decode_dms(stable: Path, raw: Path) -> None:
    from amigafs._vendor.dms import DMSError, to_adf

    try:
        image = to_adf(stable.read_bytes())
    except DMSError as exc:
        raise UnsupportedImageError(
            _("The DiskMasher archive could not be unpacked: {error}").format(error=exc)
        ) from exc
    except Exception as exc:
        raise UnsupportedImageError(
            _("The DiskMasher archive is damaged: {error}").format(error=exc)
        ) from exc
    if not image or len(image) % SECTOR_BYTES:
        raise UnsupportedImageError(_("The DiskMasher archive does not contain a whole disk."))
    # DiskMasher may omit trailing empty cylinders. AmigaDOS locates the root
    # block from the size of the disk, so the standard size is restored.
    for size in (DD_IMAGE_BYTES, HD_IMAGE_BYTES):
        if len(image) <= size:
            image = image.ljust(size, b"\0")
            break
    _write_private(raw, image)


def decode_extended_adf(data: bytes) -> bytes:
    """Rebuild a sector image from an extended ADF that holds only standard tracks."""

    magic = data[:8]
    if magic == EXTENDED_ADF_V2:
        if len(data) < 12:
            raise UnsupportedImageError(_("The extended ADF header is truncated."))
        count = struct.unpack_from(">H", data, 10)[0]
        table = 12
        entry = 12
        tracks = []
        for index in range(count):
            offset = table + index * entry
            if offset + entry > len(data):
                raise UnsupportedImageError(_("The extended ADF track table is truncated."))
            _reserved, kind, length, _bits = struct.unpack_from(">HHII", data, offset)
            tracks.append((kind, length))
        cursor = table + count * entry
    elif magic == EXTENDED_ADF_V1:
        count = 160
        table = 8
        tracks = []
        for index in range(count):
            offset = table + index * 4
            if offset + 4 > len(data):
                raise UnsupportedImageError(_("The extended ADF track table is truncated."))
            sync, length = struct.unpack_from(">HH", data, offset)
            tracks.append((0 if sync == 0 else 1, length))
        cursor = table + count * 4
    else:
        raise UnsupportedImageError(_("The file is not an extended ADF."))
    if not 1 <= count <= 2 * 84:
        raise UnsupportedImageError(_("The extended ADF declares an impossible track count."))
    lengths = {length for kind, length in tracks if length}
    if any(kind != 0 for kind, length in tracks if length):
        raise UnsupportedImageError(
            _(
                "The extended ADF holds raw or copy-protected tracks and cannot be mounted "
                "without losing track-level data."
            )
        )
    if lengths - {DD_TRACK_BYTES, HD_TRACK_BYTES} or len(lengths) != 1:
        raise UnsupportedImageError(
            _("The extended ADF does not hold one standard AmigaDOS track size.")
        )
    track_bytes = lengths.pop()
    total_tracks = max(count, 160)
    image = bytearray(total_tracks * track_bytes)
    for index, (_kind, length) in enumerate(tracks):
        if cursor + length > len(data):
            raise UnsupportedImageError(_("The extended ADF track data is truncated."))
        image[index * track_bytes : index * track_bytes + length] = data[cursor : cursor + length]
        cursor += length
    return bytes(image)


def _write_private(path: Path, data: bytes) -> None:
    with path.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


HFE_BITRATE_OFFSET = 12
HFE_HIGH_DENSITY_KBPS = 400


def hfe_density(path: Path) -> FloppyFormat | None:
    """Read the density an HFE header declares, from its bit rate."""

    header = read_magic(path, HFE_BITRATE_OFFSET + 2)
    if len(header) < HFE_BITRATE_OFFSET + 2 or header[:8] not in {
        HFE_V1_SIGNATURE,
        HFE_V3_SIGNATURE,
    }:
        return None
    rate = int.from_bytes(header[HFE_BITRATE_OFFSET : HFE_BITRATE_OFFSET + 2], "little")
    if not 100 <= rate <= 1000:
        return None
    sectors = 22 if rate >= HFE_HIGH_DENSITY_KBPS else 11
    return next(item for item in FLOPPY_FORMATS if item.sectors_per_track == sectors)


def _probe_density(stable: Path, directory: Path) -> FloppyFormat | None:
    """Decode only the first track to learn which density the image holds.

    Decoding a whole image with the wrong density takes many seconds and finds
    nothing, so one track is tried with each layout first. An HFE declares its
    bit rate, which avoids even that: Greaseweazle takes several seconds just
    to parse an HFEv3 container.
    """

    declared = hfe_density(stable)
    if declared is not None:
        return declared
    for index, floppy_format in enumerate(FLOPPY_FORMATS):
        probe = directory / f"probe-{index}.adf"
        probe.unlink(missing_ok=True)
        try:
            output = run_greaseweazle(
                [
                    "convert",
                    "--tracks=c=0:h=0",
                    f"--format={floppy_format.greaseweazle_name}",
                    str(stable),
                    str(probe),
                ]
            )
        except UnsupportedImageError:
            raise
        except AmigaFSError:
            continue
        finally:
            probe.unlink(missing_ok=True)
        if sectors_complete(output):
            return floppy_format
    return None


def _decode_flux(stable: Path, directory: Path, raw: Path) -> FloppyFormat:
    floppy_format = _probe_density(stable, directory)
    detail = _("no complete standard AmigaDOS layout matched")
    if floppy_format is not None:
        candidate = directory / "candidate.adf"
        candidate.unlink(missing_ok=True)
        try:
            output = run_greaseweazle(
                [
                    "convert",
                    f"--format={floppy_format.greaseweazle_name}",
                    str(stable),
                    str(candidate),
                ]
            )
            if (
                sectors_complete(output)
                and candidate.is_file()
                and candidate.stat().st_size == floppy_format.size
            ):
                os.chmod(candidate, 0o600)
                os.replace(candidate, raw)
                return floppy_format
        except UnsupportedImageError:
            raise
        except AmigaFSError as exc:
            detail = str(exc)
        finally:
            candidate.unlink(missing_ok=True)
    raise UnsupportedImageError(
        _(
            "The track-level image cannot be mounted without losing track-level data: {detail}. "
            "It can still be written directly to a physical floppy."
        ).format(detail=detail)
    )


def decode_container(
    source: str | Path,
    *,
    kind: str,
    directory: Path | None = None,
    progress: ProgressCallback | None = None,
) -> Workspace:
    """Decode one container into a private raw sector image.

    ``directory`` is a retained checkpoint directory for a writable session.
    Without it the working copy lives in a temporary directory that is removed
    when the workspace closes.
    """

    from amigafs.core.blockio import ImageStore

    path = Path(source).expanduser().resolve(strict=True)
    limit = MAX_FLUX_BYTES if kind in {"hfe", "scp", "ipf"} else max_workspace_bytes()
    if kind in {"dms", "extended-adf"}:
        limit = MAX_ARCHIVE_BYTES
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if directory is None:
        temporary = tempfile.TemporaryDirectory(prefix="amigafs-workspace-")
        root = Path(temporary.name)
    else:
        root = directory
    raw = root / "workspace.img"
    stable = root / f"source{path.suffix.casefold() or '.bin'}"
    report = progress or (lambda _percent, _detail: None)
    try:
        raw.unlink(missing_ok=True)
        stable.unlink(missing_ok=True)
        locked = ImageStore.open(path, writable=False)
        try:
            signature = source_signature(locked.handle.fileno(), path)
            if locked.size > limit:
                raise UnsupportedImageError(
                    _("The image exceeds the {limit} byte safety limit for its format.").format(
                        limit=limit
                    )
                )
            report(10, _("Taking a stable copy of the image…"))
            with stable.open("xb") as target:
                os.fchmod(target.fileno(), 0o600)
                offset = 0
                while offset < locked.size:
                    chunk = locked.read(offset, min(_COPY_BYTES, locked.size - offset))
                    target.write(chunk)
                    offset += len(chunk)
            if source_signature(locked.handle.fileno(), path) != signature:
                raise AmigaFSError(_("The image changed while AmigaFS was preparing it."))
        finally:
            locked.close()
        report(30, _("Decoding the image…"))
        workspace: Workspace
        if kind == "gzip":
            _decode_gzip(stable, raw)
            workspace = GzipWorkspace(path, raw, signature, temporary=temporary)
        elif kind == "dms":
            _decode_dms(stable, raw)
            workspace = ArchiveWorkspace(path, raw, signature, temporary=temporary, kind="dms")
        elif kind == "extended-adf":
            _write_private(raw, decode_extended_adf(stable.read_bytes()))
            workspace = ArchiveWorkspace(
                path, raw, signature, temporary=temporary, kind="extended-adf"
            )
        elif kind in {"hfe", "scp", "ipf"}:
            floppy_format = _decode_flux(stable, root, raw)
            workspace = FluxWorkspace(
                path,
                raw,
                signature,
                kind=kind,
                floppy_format=floppy_format,
                version=hfe_version(stable) if kind == "hfe" else None,
                temporary=temporary,
            )
        else:
            raise UnsupportedImageError(_("Unknown image container: {kind}").format(kind=kind))
        stable.unlink(missing_ok=True)
        report(100, _("Image decoded."))
        return workspace
    except BaseException:
        raw.unlink(missing_ok=True)
        stable.unlink(missing_ok=True)
        if temporary is not None:
            temporary.cleanup()
        raise


__all__ = [
    "DD_IMAGE_BYTES",
    "FLOPPY_FORMATS",
    "FloppyFormat",
    "FluxWorkspace",
    "GzipWorkspace",
    "HD_IMAGE_BYTES",
    "Workspace",
    "container_kind",
    "decode_container",
    "decode_extended_adf",
    "floppy_format_for_size",
    "hfe_density",
    "hfe_version",
    "max_workspace_bytes",
    "read_magic",
    "replace_preserving_identity",
    "run_greaseweazle",
    "sectors_complete",
    "source_signature",
    "tool_environment",
]
