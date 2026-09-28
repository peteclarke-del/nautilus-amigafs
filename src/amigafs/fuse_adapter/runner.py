"""FUSE mount lifecycle."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pyfuse3
import trio

from amigafs.core.image import AmigaImage
from amigafs.errors import AmigaFSError
from amigafs.fuse_adapter.operations import AmigaOperations
from amigafs.i18n import _
from amigafs.mounts import mount_source_name, register_mount, unregister_mount


def _contains_keyboard_interrupt(error: BaseException) -> bool:
    if isinstance(error, KeyboardInterrupt):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_contains_keyboard_interrupt(child) for child in error.exceptions)
    return False


def mount_image(
    image_path: str | Path,
    mountpoint: str | Path,
    *,
    read_write: bool = False,
    debug: bool = False,
    progress: Callable[[int, str], None] | None = None,
    write_back_started: Callable[[str], None] | None = None,
) -> None:
    """Mount one image in the foreground until interrupted or unmounted."""

    target = Path(mountpoint).expanduser().resolve()
    if not target.is_dir():
        raise AmigaFSError(
            _("Mountpoint does not exist or is not a directory: {path}").format(path=target)
        )
    try:
        if any(target.iterdir()):
            raise AmigaFSError(_("Mountpoint must be empty: {path}").format(path=target))
    except OSError as exc:
        raise AmigaFSError(
            _("Cannot inspect mountpoint {path}: {error}").format(path=target, error=exc)
        ) from exc

    registered = False
    image = AmigaImage.open(image_path, writable=read_write, progress=progress)
    clean = False
    try:
        operations = AmigaOperations(image)
        options = set(pyfuse3.default_options)
        options.update(
            {
                "nodev",
                "nosuid",
                "noexec",
                "auto_unmount",
                "subtype=amigafs",
                f"fsname={mount_source_name(image.source.name)}",
            }
        )
        if not read_write:
            options.add("ro")
        if debug:
            logging.basicConfig(level=logging.DEBUG)
            options.add("debug")
        # Publish identity before init: the kernel mount may become visible
        # while libfuse is still completing initialisation. active_mounts()
        # remains kernel-gated, so this cannot advertise a mount early.
        register_mount(image.source, target, read_write=read_write)
        registered = True
        pyfuse3.init(operations, str(target), options)
        try:
            trio.run(pyfuse3.main)
        except BaseException as exc:
            if not _contains_keyboard_interrupt(exc):
                pyfuse3.close(unmount=False)
                raise
        try:
            operations.flush_pending()
        finally:
            pyfuse3.close()
        clean = True
    finally:
        try:
            if clean and image.needs_write_back and write_back_started is not None:
                write_back_started(image.source.name)
            image.close(clean=clean, progress=progress)
        finally:
            if registered:
                unregister_mount(target)


def mount_read_only(image_path: str | Path, mountpoint: str | Path, *, debug: bool = False) -> None:
    """Compatibility wrapper for an explicitly read-only mount."""

    mount_image(image_path, mountpoint, debug=debug)
