"""Copying a whole physical disc to an image file, and an image to a disc.

Reading is safe and never alters the disc. Writing replaces everything on the
destination and cannot be undone, so it demands the exact device name as
confirmation, refuses a mounted or in-use disc through the device policy, and
reads the whole disc back afterwards to prove what was written.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from amigafs.core.blockio import media_size
from amigafs.core.devices import describe_disc, open_device
from amigafs.core.formats import resolve_image
from amigafs.errors import AmigaFSError
from amigafs.i18n import _
from amigafs.operations import (
    CancellationCheck,
    ProgressCallback,
    cancellation_point,
    report_progress,
)

COPY_BYTES = 4 * 1024 * 1024
HOLE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class DiscTransferResult:
    device: str
    image: Path
    size: int
    sha256: str


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_disc(
    device: str | Path,
    destination: str | Path,
    *,
    progress: ProgressCallback | None = None,
    cancelled: CancellationCheck | None = None,
) -> DiscTransferResult:
    """Copy a whole physical Amiga disc to a new image, never overwriting a file."""

    disc = describe_disc(device)
    requested = Path(destination).expanduser()
    parent = requested.parent.resolve()
    target = parent / requested.name
    if not parent.is_dir():
        raise AmigaFSError(
            _("The destination directory does not exist: {path}").format(path=parent)
        )
    if any(child.name.casefold() == target.name.casefold() for child in parent.iterdir()):
        raise AmigaFSError(_("Refusing to overwrite an existing file: {path}").format(path=target))
    report_progress(progress, 0, _("Opening {name}…").format(name=disc.model or disc.name))
    descriptor = open_device(disc.device, writable=False)
    staged = parent / f".{target.name}.amigafs-{os.getpid()}"
    digest = hashlib.sha256()
    try:
        size = media_size(descriptor)
        if shutil.disk_usage(parent).free < size:
            raise AmigaFSError(
                _("The destination has less than the {size} bytes this disc needs.").format(
                    size=size
                )
            )
        with staged.open("xb") as output:
            offset = 0
            while offset < size:
                cancellation_point(cancelled)
                chunk = os.pread(descriptor, min(COPY_BYTES, size - offset), offset)
                if not chunk:
                    raise AmigaFSError(
                        _("The disc stopped answering at byte {offset}.").format(offset=offset)
                    )
                digest.update(chunk)
                # Runs of empty sectors become holes, so the image of a mostly
                # empty disc does not occupy the disc's whole capacity.
                for start in range(0, len(chunk), HOLE_BYTES):
                    piece = chunk[start : start + HOLE_BYTES]
                    if piece.count(0) == len(piece):
                        output.seek(len(piece), os.SEEK_CUR)
                    else:
                        output.write(piece)
                offset += len(chunk)
                report_progress(
                    progress,
                    offset * 95 // size,
                    _("Reading the disc… {done} of {total} MiB").format(
                        done=offset // (1024 * 1024), total=size // (1024 * 1024)
                    ),
                )
            output.truncate(size)
            output.flush()
            os.fsync(output.fileno())
        os.link(staged, target)
        _sync_directory(parent)
    except OSError as exc:
        raise AmigaFSError(_("Could not read the disc: {error}").format(error=exc)) from exc
    finally:
        os.close(descriptor)
        staged.unlink(missing_ok=True)
    report_progress(progress, 100, _("Disc image saved."))
    return DiscTransferResult(
        device=disc.device, image=target, size=size, sha256=digest.hexdigest()
    )


def write_disc(
    image: str | Path,
    device: str | Path,
    *,
    confirmation: str,
    progress: ProgressCallback | None = None,
) -> DiscTransferResult:
    """Replace the whole content of a physical disc with a hard-disc image."""

    source = resolve_image(image)
    if source.kind != "hard-disc-image":
        raise AmigaFSError(_("Only a plain Amiga hard-disc image can be written to a disc."))
    disc = describe_disc(device)
    if confirmation != disc.name:
        raise AmigaFSError(
            _("Confirmation must exactly match the device name: {name}").format(name=disc.name)
        )
    from amigafs.mounts import mount_for_image_path
    from amigafs.recovery import pending_recovery

    if mount_for_image_path(source.primary_path) is not None:
        raise AmigaFSError(_("Unmount the image before writing it to a disc."))
    if pending_recovery(disc.stable_path) is not None:
        raise AmigaFSError(
            _("The disc has an interrupted writable session; resolve its recovery first.")
        )
    report_progress(progress, 0, _("Opening {name}…").format(name=disc.model or disc.name))
    descriptor = open_device(disc.device, writable=True, allow_blank=True)
    digest = hashlib.sha256()
    try:
        with source.primary_path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            size = before.st_size
            capacity = media_size(descriptor)
            if size > capacity:
                raise AmigaFSError(
                    _("The image is {size} bytes but the disc holds only {capacity}.").format(
                        size=size, capacity=capacity
                    )
                )
            offset = 0
            while offset < size:
                chunk = handle.read(min(COPY_BYTES, size - offset))
                if not chunk:
                    raise AmigaFSError(_("The image ended before its recorded length."))
                digest.update(chunk)
                view = memoryview(chunk)
                position = offset
                while view:
                    written = os.pwrite(descriptor, view, position)
                    view = view[written:]
                    position += written
                offset += len(chunk)
                report_progress(
                    progress,
                    offset * 60 // size,
                    _("Writing the disc… {done} of {total} MiB").format(
                        done=offset // (1024 * 1024), total=size // (1024 * 1024)
                    ),
                )
            os.fsync(descriptor)
            after = os.fstat(handle.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise AmigaFSError(
                    _("The image changed while it was being written; the disc is not reliable.")
                )
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
        verify = hashlib.sha256()
        offset = 0
        while offset < size:
            chunk = os.pread(descriptor, min(COPY_BYTES, size - offset), offset)
            if not chunk:
                break
            verify.update(chunk)
            offset += len(chunk)
            report_progress(progress, 60 + offset * 39 // size, _("Verifying the disc…"))
        if offset != size or verify.digest() != digest.digest():
            raise AmigaFSError(
                _("The disc does not read back as it was written. Do not rely on its contents.")
            )
    except OSError as exc:
        raise AmigaFSError(
            _("Could not write the disc: {error}. It may be incomplete.").format(error=exc)
        ) from exc
    finally:
        os.close(descriptor)
    report_progress(progress, 100, _("Disc written and verified."))
    return DiscTransferResult(
        device=disc.device, image=source.primary_path, size=size, sha256=digest.hexdigest()
    )


__all__ = ["DiscTransferResult", "read_disc", "write_disc"]
