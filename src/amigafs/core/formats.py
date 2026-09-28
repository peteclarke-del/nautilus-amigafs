"""Cheap, content-driven source identification and capability selection.

Resolving a source never decodes, unpacks or reads a physical medium. It looks
at the first few kilobytes of a file, or at the kernel's description of a
device, and answers which operations are safe to offer. The filesystem inside a
container is established later, from its decoded sectors, by
:mod:`amigafs.core.media`.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from amigafs.core.containers import (
    DD_IMAGE_BYTES,
    HD_IMAGE_BYTES,
    SECTOR_BYTES,
    container_kind,
    read_magic,
)
from amigafs.core.device_policy import RDB_SEARCH_BLOCKS, amiga_evidence
from amigafs.errors import UnsupportedImageError
from amigafs.i18n import _

FLOPPY_SCHEME = "floppy"
FLOPPY_DRIVES = ("A", "B", "0", "1", "2", "3")
KICKSTART_MINIMUM_BYTES = 8 * 1024
KICKSTART_MAXIMUM_BYTES = 0x1000000
_FLOPPY_REFERENCE = re.compile(r"^(?:floppy|greaseweazle):(?P<drive>[A-Za-z0-9])$")


@dataclass(frozen=True, slots=True)
class ImageCapabilities:
    """Operations safe for one detected source profile."""

    mount_read_only: bool
    mount_read_write: bool
    validate: bool
    repair: bool
    recover: bool
    properties: bool
    file_forge: bool
    write_floppy: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedImage:
    """Canonical source identity plus the profile needed to open it."""

    primary_path: Path
    kind: str
    capabilities: ImageCapabilities
    container: str | None = None
    is_device: bool = False
    drive: str | None = None
    display_name: str = ""
    case_sensitive_names: bool = False

    @property
    def identity_paths(self) -> tuple[Path, ...]:
        return (self.primary_path,)

    @property
    def name(self) -> str:
        return self.display_name or self.primary_path.name

    def close(self) -> None:
        """Resolution holds no resources; kept for callers that pair it with open."""


#                                       ro    rw     valid  repair recov  props  forge  floppy
_SECTOR_FLOPPY = ImageCapabilities(True, True, True, True, True, True, True, True)
_HARD_DISC = ImageCapabilities(True, True, True, True, True, True, True, False)
_PHYSICAL_DISC = ImageCapabilities(True, True, True, True, True, True, False, False)
_PHYSICAL_FLOPPY = ImageCapabilities(True, True, True, False, True, True, False, False)
_GZIP = ImageCapabilities(True, True, True, False, True, True, True, True)
_DMS = ImageCapabilities(True, False, True, False, False, True, True, True)
_EXTENDED_ADF = ImageCapabilities(True, False, True, False, False, True, True, False)
_HFE = ImageCapabilities(True, True, True, False, True, True, True, True)
_FLUX_READ_ONLY = ImageCapabilities(True, False, True, False, False, True, True, True)
_KICKSTART = ImageCapabilities(True, False, True, False, False, True, True, False)

_CONTAINER_PROFILES = {
    "gzip": ("compressed-image", _GZIP),
    "dms": ("dms-archive", _DMS),
    "extended-adf": ("extended-adf", _EXTENDED_ADF),
    "hfe": ("flux-image", _HFE),
    "scp": ("flux-image", _FLUX_READ_ONLY),
    "ipf": ("flux-image", _FLUX_READ_ONLY),
}

_SUFFIX_CAPABILITIES = {
    ".adf": _SECTOR_FLOPPY,
    ".adz": _GZIP,
    ".dms": _DMS,
    ".hdf": _HARD_DISC,
    ".hda": _HARD_DISC,
    ".hdz": _GZIP,
    ".rdsk": _HARD_DISC,
    ".hfe": _HFE,
    ".scp": _FLUX_READ_ONLY,
    ".ipf": _FLUX_READ_ONLY,
    ".rom": _KICKSTART,
    ".kick": _KICKSTART,
}

#: Suffixes AmigaFS claims on the desktop. ``.img`` and ``.raw`` are opened when
#: asked but never claimed, because most such files are not Amiga media.
DESKTOP_SUFFIXES = frozenset(_SUFFIX_CAPABILITIES)
PROBE_SUFFIXES = DESKTOP_SUFFIXES | {".img", ".raw", ".dsk", ".gz"}


def floppy_reference(selected: str | os.PathLike[str]) -> str | None:
    """Return the drive named by ``floppy:A``, or ``None`` for any other reference."""

    if isinstance(selected, Path):
        return None
    match = _FLOPPY_REFERENCE.fullmatch(os.fspath(selected).strip())
    if match is None:
        return None
    drive = match["drive"].upper()
    return drive if drive in FLOPPY_DRIVES else None


def floppy_identity_path(drive: str) -> Path:
    """Return the private token file whose inode and lock identify one drive."""

    from amigafs.mounts import runtime_root
    from amigafs.safe_paths import ensure_private_directory

    root = runtime_root()
    directory = root / "floppy"
    ensure_private_directory(directory, anchor=root.parent)
    token = directory / f"drive-{drive}"
    descriptor = os.open(token, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    os.close(descriptor)
    return token


def floppy_drive_for_path(path: str | Path) -> str | None:
    """Recognise a floppy token path and return its drive."""

    candidate = Path(path)
    if candidate.parent.name != "floppy" or not candidate.name.startswith("drive-"):
        return None
    drive = candidate.name.removeprefix("drive-")
    if drive not in FLOPPY_DRIVES:
        return None
    from amigafs.mounts import runtime_root

    try:
        if candidate.parent.resolve() != (runtime_root() / "floppy").resolve():
            return None
    except OSError:
        return None
    return drive


def image_capabilities_hint(selected: str | Path) -> ImageCapabilities | None:
    """Return filename-level capabilities without opening the selected file.

    Desktop integrations use this conservative hint while constructing menus.
    The invoked operation still performs full content-driven resolution before
    reading or modifying an image.
    """

    return _SUFFIX_CAPABILITIES.get(Path(selected).suffix.casefold())


def looks_like_kickstart(path: Path, size: int) -> bool:
    """Return whether a small file decodes as a Kickstart ROM.

    A ROM has to fit the 68000's 24-bit address space, so anything larger is
    never read for this test.
    """

    if not KICKSTART_MINIMUM_BYTES <= size <= KICKSTART_MAXIMUM_BYTES:
        return False
    from amigafs._vendor.amiganut.kickfs.kickfs import Kickstart

    try:
        Kickstart.from_bytes(path.read_bytes())
    except Exception:
        return False
    return True


def _floppy(drive: str) -> ResolvedImage:
    return ResolvedImage(
        primary_path=floppy_identity_path(drive),
        kind="physical-floppy",
        capabilities=_PHYSICAL_FLOPPY,
        container="greaseweazle",
        drive=drive,
        display_name=_("Floppy drive {drive}").format(drive=drive),
    )


def resolve_image(selected: str | Path) -> ResolvedImage:
    """Resolve a supported source from its header, without decoding it."""

    drive = floppy_reference(selected)
    if drive is not None:
        return _floppy(drive)
    selected_path = Path(selected).expanduser()
    token_drive = floppy_drive_for_path(selected_path)
    if token_drive is not None:
        return _floppy(token_drive)
    try:
        details = selected_path.stat()
    except OSError as exc:
        raise UnsupportedImageError(
            _("Image does not exist or cannot be inspected: {path}").format(path=selected_path)
        ) from exc
    if stat.S_ISBLK(details.st_mode):
        from amigafs.core.devices import describe_disc
        from amigafs.recovery import canonical_source

        disc = describe_disc(selected_path)
        return ResolvedImage(
            primary_path=canonical_source(disc.stable_path),
            kind="physical-disc",
            capabilities=_PHYSICAL_DISC,
            is_device=True,
            display_name=disc.model or disc.name,
        )
    if not stat.S_ISREG(details.st_mode):
        raise UnsupportedImageError(
            _("Image does not exist or is not a regular file: {path}").format(path=selected_path)
        )
    path = selected_path.resolve()
    container = container_kind(path)
    if container is not None:
        kind, capabilities = _CONTAINER_PROFILES[container]
        return ResolvedImage(
            primary_path=path, kind=kind, capabilities=capabilities, container=container
        )
    size = details.st_size
    head = read_magic(path, RDB_SEARCH_BLOCKS * SECTOR_BYTES)
    evidence = amiga_evidence(head) if size and size % SECTOR_BYTES == 0 else ""
    if not evidence and looks_like_kickstart(path, size):
        return ResolvedImage(
            primary_path=path,
            kind="kickstart-rom",
            capabilities=_KICKSTART,
            case_sensitive_names=True,
        )
    if not evidence:
        raise UnsupportedImageError(
            _("The file is not a supported AmigaFS image: {path}").format(path=path)
        )
    if evidence == "volume" and size in {DD_IMAGE_BYTES, HD_IMAGE_BYTES}:
        return ResolvedImage(primary_path=path, kind="floppy-image", capabilities=_SECTOR_FLOPPY)
    return ResolvedImage(primary_path=path, kind="hard-disc-image", capabilities=_HARD_DISC)


__all__ = [
    "DESKTOP_SUFFIXES",
    "FLOPPY_DRIVES",
    "ImageCapabilities",
    "PROBE_SUFFIXES",
    "ResolvedImage",
    "floppy_drive_for_path",
    "floppy_identity_path",
    "floppy_reference",
    "image_capabilities_hint",
    "looks_like_kickstart",
    "resolve_image",
]
