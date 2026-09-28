"""Race-resistant creation of AmigaFS-owned private directories."""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from amigafs.errors import AmigaFSError
from amigafs.i18n import _


@contextmanager
def _private_directory_descriptor(path: Path, *, anchor: Path) -> Iterator[int]:
    """Yield a locked traversal result without reopening descendant paths."""

    try:
        parts = path.relative_to(anchor).parts
    except ValueError as exc:
        raise AmigaFSError(_("A private AmigaFS directory escaped its trusted root.")) from exc
    if not parts:
        raise AmigaFSError(_("A private AmigaFS directory must be below its trusted root."))
    anchor.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(anchor, flags)
    except OSError as exc:
        raise AmigaFSError(_("The trusted AmigaFS directory root is unsafe.")) from exc
    try:
        for part in parts:
            if part in {"", ".", ".."} or "/" in part:
                raise AmigaFSError(_("A private AmigaFS directory name is unsafe."))
            with suppress(FileExistsError):
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise AmigaFSError(
                    _("Refusing an unsafe private AmigaFS directory: {name}").format(name=part)
                ) from exc
            metadata = os.fstat(child)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                os.close(child)
                raise AmigaFSError(
                    _("Refusing an unsafe private AmigaFS directory: {name}").format(name=part)
                )
            os.close(descriptor)
            descriptor = child
            os.fchmod(descriptor, 0o700)
        yield descriptor
    finally:
        os.close(descriptor)


def ensure_private_directory(path: Path, *, anchor: Path) -> Path:
    """Create descendants below a trusted anchor without following symlinks."""

    with _private_directory_descriptor(path, anchor=anchor):
        pass
    return path


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        try:
            written = os.write(descriptor, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("private state write made no progress")
        view = view[written:]


def _fsync(descriptor: int) -> None:
    while True:
        try:
            os.fsync(descriptor)
            return
        except InterruptedError:
            continue


def atomic_write_private_text(
    path: Path,
    content: str,
    *,
    anchor: Path,
    mode: int = 0o600,
) -> None:
    """Durably replace one private text file without following path components."""

    if path.name in {"", ".", ".."} or path.parent == path:
        raise AmigaFSError(_("A private AmigaFS state filename is unsafe."))
    encoded = content.encode("utf-8")
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    with _private_directory_descriptor(path.parent, anchor=anchor) as directory:
        temporary_descriptor: int | None = None
        temporary_created = False
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            temporary_descriptor = os.open(
                temporary_name,
                flags,
                mode,
                dir_fd=directory,
            )
            temporary_created = True
            _write_all(temporary_descriptor, encoded)
            os.fchmod(temporary_descriptor, mode)
            _fsync(temporary_descriptor)
            os.close(temporary_descriptor)
            temporary_descriptor = None
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
            )
            temporary_created = False
            _fsync(directory)
        except BaseException:
            if temporary_descriptor is not None:
                with suppress(OSError):
                    os.close(temporary_descriptor)
            if temporary_created:
                with suppress(OSError):
                    os.unlink(temporary_name, dir_fd=directory)
            raise


__all__ = ["atomic_write_private_text", "ensure_private_directory"]
