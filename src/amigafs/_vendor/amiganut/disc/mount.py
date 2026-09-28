"""Turning ``image.adf:C/List`` into a mounted volume and an inner path.

Every workbench operation names one place inside one image. Rather than pass a
file path and an inner path separately through a dozen call sites, the engine
accepts a single *compound path*: the host file, a colon, then the AmigaDOS
path inside it. That is the same shape an Amiga user types at a shell prompt,
and it means a command can be logged, repeated and reasoned about as one
string.

Splitting is done by testing candidate prefixes against the filesystem rather
than by finding the first colon, because a host directory may legitimately
contain one and an inner path may legitimately begin with one.
"""

from __future__ import annotations

import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..errors import ConfigurationError, DataError
from ..filesystem import (
    RigidDiskMount,
    create_filesystem,
    geometry_from_geo,
    identify,
    reader_for,
)
from ..filesystem.amigados import join_path, split_path


@dataclass
class ResolvedMount:
    """A mounted volume and the inner path the caller asked about."""

    mount: object
    path: str
    image: Path
    filesystem: str

    @property
    def parts(self) -> list[str]:
        return split_path(self.path)


def _is_image(path: Path) -> bool:
    """Whether a path holds an image: a regular file or a whole block device.

    A drive attached to the host is opened in place through its device node,
    which is not a regular file but is read the same way.
    """
    try:
        return path.is_file() or stat.S_ISBLK(path.stat().st_mode)
    except OSError:
        return False


def split_compound(compound: str) -> tuple[Path, str]:
    """Split ``image.adf:C/List`` into its host path and inner path."""
    text = str(compound)
    if not text:
        raise ConfigurationError("An empty path names nothing.")
    candidate = Path(text)
    if _is_image(candidate):
        return candidate, ""
    # Walk the colons from the right so the longest existing file wins. A
    # trailing colon means "the root of this volume", which is how AmigaDOS
    # spells a device root.
    positions = [index for index, character in enumerate(text) if character == ":"]
    for index in reversed(positions):
        host = Path(text[:index])
        if _is_image(host):
            return host, text[index + 1 :]
    if positions:
        host = Path(text[: positions[0]])
        return host, text[positions[0] + 1 :]
    return candidate, ""


def _geometry_sidecar(image: Path):
    """Load a ``.geo`` sidecar written beside an RDB-less hardfile."""
    for candidate in (
        image.with_suffix(image.suffix + ".geo"),
        image.with_suffix(".geo"),
    ):
        if candidate.is_file():
            return geometry_from_geo(candidate.read_bytes())
    return None


def mount_image(
    image: Path | str,
    *,
    writable: bool = False,
    filesystem: str | None = None,
    partition: int | None = None,
):
    """Mount an image, choosing the filing system by content when not told."""
    image = Path(image)
    if not _is_image(image):
        raise DataError(f"{image} does not exist.")
    name = filesystem
    if name is None:
        candidates = identify(image, suffix_hint=image.suffix.lower())
        if not candidates:
            raise DataError(
                "No AmigaDOS filing system was found in these bytes. Supply the raw, "
                "uncompressed image rather than an emulator wrapper, an archive member "
                "or a flux capture. This build reads OFS and FFS volumes "
                "(DOS\\0 to DOS\\7), SFS and PFS3 volumes, RDB partitioned hard "
                "drives and Kickstart ROMs."
            )
        name = candidates[0].filesystem
    driver = create_filesystem(name)
    reader = reader_for(image, writable=writable)
    try:
        mount = driver.open(reader, _geometry_sidecar(image))
    except Exception:
        reader.close()
        raise
    if isinstance(mount, RigidDiskMount) and partition is not None:
        # The partition mount opens its own handle onto its part of the drive,
        # so the one used to read the partition table is not kept open.
        try:
            return mount.open_partition(partition, writable=writable), name
        finally:
            mount.close()
    return mount, name


@contextmanager
def resolve_mount(
    compound: str,
    *,
    writable: bool = False,
    filesystem: str | None = None,
    partition: int | None = None,
):
    """Mount the image named by a compound path and yield it with its inner path."""
    image, inner = split_compound(compound)
    mount, name = mount_image(
        image, writable=writable, filesystem=filesystem, partition=partition
    )
    try:
        yield ResolvedMount(
            mount=mount,
            path=join_path(split_path(inner)),
            image=image,
            filesystem=name,
        )
    finally:
        close = getattr(mount, "close", None)
        if callable(close):
            close()


__all__ = [
    "ResolvedMount",
    "mount_image",
    "resolve_mount",
    "split_compound",
]
