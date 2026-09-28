"""The filesystem registry, identification cascade and mount protocols.

Amiga File Forge only ever asks this package three things: give me a reader
for this image, tell me what is on it, and mount it. Everything else is
reached through the mount object those calls return.

The protocol classes at the bottom are deliberately structural. A mount
advertises that it carries protection bits by subclassing ``AmigaMetadata``,
so listing code can ask ``isinstance(mount, AmigaMetadata)`` without knowing
which filing system produced it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..errors import ConfigurationError, DataError
from ..file import Access, AmigaMeta
from .amigados import (
    AmigaDOSVolume,
    Entry,
    Stat,
    format_volume,
    join_path,
    split_path,
    validate_name,
)
from .blocks import (
    BLOCK_SIZE,
    DD_BLOCKS,
    DOS_TYPES,
    HD_BLOCKS,
    NAMED_GEOMETRIES,
    RESERVED_BLOCKS,
    BlockReader,
    Geometry,
)
from .pfs3 import PFS3Volume
from .pfs3_blocks import PFS3_DOS_TYPES, ROOT_IDS as PFS3_ROOT_IDS
from .sfs import SFS_ID, SFSVolume
from .rdb import (
    FileSystemHandler,
    Partition,
    RigidDisk,
    find_rdb_block,
    partition_reader,
    read_rigid_disk,
    write_rigid_disk,
)


# ---------------------------------------------------------------------------
# Mount protocols
# ---------------------------------------------------------------------------
class AmigaMetadata:
    """A mount whose entries carry protection bits and a comment."""

    def amiga_meta(self, path: str) -> AmigaMeta:  # pragma: no cover - protocol
        raise NotImplementedError


class Datestamped:
    """A mount whose entries carry a datestamp."""

    def datestamp(self, path: str) -> datetime | None:  # pragma: no cover
        raise NotImplementedError


class Filetyped:
    """A mount that can report a Workbench object type for an entry."""

    def filetype(self, path: str) -> int | None:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------
def reader_for(path: Path | str, *, writable: bool = False, block_size: int = BLOCK_SIZE) -> BlockReader:
    """Open an image file for block access."""
    return BlockReader(path, writable=writable, block_size=block_size)


# ---------------------------------------------------------------------------
# Geometry sidecars
# ---------------------------------------------------------------------------
GEOMETRY_KEYS = {
    "surfaces": "surfaces",
    "heads": "surfaces",
    "blockspertrack": "blocks_per_track",
    "sectorspertrack": "blocks_per_track",
    "sectors": "blocks_per_track",
    "reserved": "reserved",
    "blocksize": "block_size",
    "sectorsize": "block_size",
    "lowcyl": "low_cylinder",
    "highcyl": "high_cylinder",
    "cylinders": "cylinders",
    "bootpri": "boot_priority",
    "dostype": "dos_type",
}


def geometry_from_geo(data: bytes | str) -> Geometry:
    """Parse a WinUAE ``.geo`` sidecar for an RDB-less hardfile.

    A hardfile carries no partition table, so the host has to be told its
    surfaces, sectors and reserved-block count. WinUAE stores that beside the
    image as ``name.hdf.geo``: plain ``key=value`` lines, optionally with
    comments. Amiga File Forge accepts the same file so an image prepared for
    an emulator opens here without being described twice.
    """
    if isinstance(data, bytes):
        text = data.decode("latin-1", "replace")
    else:
        text = str(data)
    values: dict[str, int | bytes] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].split(";", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip().lower().replace("_", "").replace(" ", "")
        field = GEOMETRY_KEYS.get(key)
        if field is None:
            continue
        raw = raw.strip()
        if field == "dos_type":
            cleaned = raw.strip("'\"")
            if re.fullmatch(r"(?:0x)?[0-9A-Fa-f]{8}", cleaned):
                values[field] = int(cleaned.removeprefix("0x"), 16).to_bytes(4, "big")
            else:
                values[field] = cleaned.encode("latin-1")[:4].ljust(4, b"\0")
            continue
        try:
            values[field] = int(raw, 0)
        except ValueError:
            continue
    if not values:
        raise DataError("The geometry sidecar does not declare any usable fields.")
    geometry = Geometry(
        surfaces=int(values.get("surfaces", 1)),
        blocks_per_track=int(values.get("blocks_per_track", 32)),
        reserved=int(values.get("reserved", RESERVED_BLOCKS)),
        block_size=int(values.get("block_size", BLOCK_SIZE)),
        low_cylinder=int(values.get("low_cylinder", 0)),
        boot_priority=int(values.get("boot_priority", 0)),
        dos_type=values.get("dos_type", b"DOS\x03"),
    )
    if "cylinders" in values:
        geometry.high_cylinder = geometry.low_cylinder + int(values["cylinders"]) - 1
    elif "high_cylinder" in values:
        geometry.high_cylinder = int(values["high_cylinder"])
    return geometry


#: Legacy alias. Amiga File Forge opens a hardfile and its geometry sidecar
#: together in the same way earlier releases opened a descriptor pair.
geometry_from_dsc = geometry_from_geo


def write_geometry(geometry: Geometry) -> str:
    """Render a ``.geo`` sidecar for a hardfile this build created."""
    return (
        f"surfaces={geometry.surfaces}\n"
        f"blockspertrack={geometry.blocks_per_track}\n"
        f"reserved={geometry.reserved}\n"
        f"blocksize={geometry.block_size}\n"
        f"lowcyl={geometry.low_cylinder}\n"
        f"highcyl={geometry.high_cylinder}\n"
        f"bootpri={geometry.boot_priority}\n"
        f"dostype={geometry.dos_type.hex().upper()}\n"
    )


# ---------------------------------------------------------------------------
# Mounts
# ---------------------------------------------------------------------------
class AmigaDOSMount(AmigaMetadata, Datestamped, Filetyped):
    """The workbench-facing view of one mounted OFS or FFS volume."""

    def __init__(self, volume: AmigaDOSVolume, name: str = "amigados"):
        self.volume = volume
        self.filesystem = name

    # ---- identity -----------------------------------------------------
    @property
    def format(self) -> str:
        return self.volume.format

    @property
    def title(self) -> str:
        return self.volume.title

    def set_title(self, value: str) -> None:
        self.volume.set_title(value)

    # ---- traversal ----------------------------------------------------
    def exists(self, path: str | None) -> bool:
        return self.volume.exists(path)

    def stat(self, path: str | None) -> Stat:
        return self.volume.stat(path)

    def iter_entries(self, path: str | None = None):
        return self.volume.iter_entries(path)

    # ---- content ------------------------------------------------------
    def read_bytes(self, path: str) -> bytes:
        return self.volume.read_bytes(path)

    def write_bytes(self, path: str, data: bytes, meta: AmigaMeta | None = None) -> None:
        self.volume.write_bytes(path, data, meta)

    def mkdir(self, path: str) -> None:
        self.volume.mkdir(path)

    def make_directory(
        self, path: str, *, parents: bool = False, exist_ok: bool = False
    ) -> None:
        """Create a directory, optionally building the chain above it."""
        parts = split_path(path)
        if not parts:
            if exist_ok:
                return
            raise DataError("The volume root already exists.")
        if parents:
            for depth in range(1, len(parts)):
                branch = join_path(parts[:depth])
                if not self.volume.exists(branch):
                    self.volume.mkdir(branch)
        target = join_path(parts)
        if self.volume.exists(target):
            if exist_ok and self.volume.stat(target).is_dir:
                return
            raise DataError(f"{target} already exists.")
        self.volume.mkdir(target)

    def _navigate(self, path: str | None):
        """Return a node view of one path, whether or not it exists yet."""
        return PathNode(self, path or "")

    def remove(self, path: str, *, recursive: bool = False, force: bool = False) -> None:
        """Delete an entry. ``force`` clears protection bits that would block it."""
        if force:
            stat = self.volume.stat(path)
            if not stat.is_dir and self.volume.access(path).locked:
                self.volume.set_access(path, 0)
            recursive = True
        self.volume.remove(path, recursive=recursive)

    def rename(self, source: str, destination: str) -> None:
        self.volume.rename(source, destination)

    # ---- metadata -----------------------------------------------------
    def amiga_meta(self, path: str) -> AmigaMeta:
        return self.volume.amiga_meta(path)

    def set_amiga_meta(self, path: str, meta: AmigaMeta) -> None:
        self.volume.set_amiga_meta(path, meta)

    def access(self, path: str) -> Access:
        return self.volume.access(path)

    def set_access(self, path: str, access: Access | int) -> None:
        self.volume.set_access(path, access)

    def datestamp(self, path: str) -> datetime | None:
        return self.volume.datestamp(path)

    def set_datestamp(self, path: str, moment: datetime) -> None:
        self.volume.set_datestamp(path, moment)

    def filetype(self, path: str) -> int | None:
        """Report the Workbench type from the entry's ``.info`` icon."""
        from ..file.filetypes import icon_name, icon_type

        icon = icon_name(path)
        if not self.volume.exists(icon):
            return None
        try:
            return icon_type(self.volume.read_bytes(icon))
        except DataError:
            return None

    def set_filetype(self, path: str, value: int | str | None) -> None:
        """Record a Workbench object type by writing or updating its icon.

        AmigaDOS keeps no type field in the catalogue, so the type lives in
        the ``.info`` file Workbench reads. Writing one here means a type set
        on an import is the same type Workbench shows on a real machine.
        """
        from ..file.filetypes import icon_name, minimal_icon, parse_filetype

        code = parse_filetype(value)
        icon = icon_name(path)
        if code is None:
            if self.volume.exists(icon):
                self.volume.remove(icon)
            return
        if self.volume.exists(icon):
            existing = bytearray(self.volume.read_bytes(icon))
            if len(existing) >= 50:
                existing[48:50] = int(code).to_bytes(2, "big")
                self.volume.write_bytes(icon, bytes(existing))
                return
        self.volume.write_bytes(icon, minimal_icon(code))

    # ---- volume-level -------------------------------------------------
    def size_bytes(self) -> int:
        return self.volume.size_bytes()

    def free_bytes(self) -> int:
        return self.volume.free_bytes()

    def boot_option(self) -> int:
        return self.volume.boot_option()

    def set_boot_option(self, option: int) -> None:
        self.volume.set_boot_option(option)

    def validate(self) -> list[str]:
        return self.volume.validate()

    def defragment(self) -> int:
        return self.volume.defragment()

    def free_map(self) -> list[bool]:
        return self.volume.free_map()

    def flush(self) -> None:
        self.volume.flush()

    def close(self) -> None:
        self.volume.close()


class SFSMount(AmigaDOSMount):
    """The workbench-facing view of one mounted Smart File System volume.

    The AmigaDOS mount only forwards to its volume, and an SFS volume answers
    the same calls, so the icon handling and path helpers are shared.
    """

    def __init__(self, volume: SFSVolume, name: str = "sfs"):
        super().__init__(volume, name)


class PFS3Mount(AmigaDOSMount):
    """The workbench-facing view of one mounted Professional File System volume."""

    def __init__(self, volume: PFS3Volume, name: str = "pfs3"):
        super().__init__(volume, name)


class PathNode:
    """One place inside a mounted volume, whether or not it exists yet.

    The workbench addresses a destination before creating it, so this node is
    deliberately lazy: it resolves nothing until asked. ``title`` reads and
    writes the entry's free-text comment, which is the nearest AmigaDOS
    equivalent of a directory title and is what a real machine shows in
    ``List``.
    """

    def __init__(self, mount: "AmigaDOSMount", path: str):
        self.mount = mount
        self.path = join_path(split_path(path))

    # ---- identity -----------------------------------------------------
    @property
    def name(self) -> str:
        parts = split_path(self.path)
        return parts[-1] if parts else self.mount.title

    @property
    def exists(self) -> bool:
        return self.mount.exists(self.path)

    @property
    def is_root(self) -> bool:
        return not split_path(self.path)

    @property
    def is_dir(self) -> bool:
        return self.exists and self.mount.stat(self.path).is_dir

    @property
    def supports_title(self) -> bool:
        """Comments are available on every AmigaDOS entry, including drawers."""
        return True

    # ---- title, which is the entry comment ----------------------------
    @property
    def title(self) -> str:
        if self.is_root:
            return self.mount.title
        if not self.exists:
            return ""
        return self.mount.amiga_meta(self.path).comment

    @title.setter
    def title(self, value: str) -> None:
        self.set_title(value)

    def set_title(self, value: str) -> None:
        if self.is_root:
            self.mount.set_title(value)
            return
        meta = self.mount.amiga_meta(self.path)
        self.mount.set_amiga_meta(self.path, meta.with_comment(str(value or "")[:79]))

    # ---- content ------------------------------------------------------
    def read_bytes(self) -> bytes:
        return self.mount.read_bytes(self.path)

    def write_bytes(
        self,
        data: bytes,
        *,
        access: Access | int | None = None,
        comment: str | None = None,
        datestamp: datetime | None = None,
    ) -> None:
        """Write content and its catalogue metadata as one update."""
        protection = 0
        if access is not None:
            protection = access.value if isinstance(access, Access) else int(access)
        meta = AmigaMeta(
            protection=protection,
            comment=str(comment or ""),
            datestamp=datestamp,
        )
        self.mount.write_bytes(self.path, data, meta)

    def chmod(self, value: Access | int) -> None:
        self.mount.set_access(self.path, value)

    def make_directory(self, *, parents: bool = False, exist_ok: bool = False) -> None:
        self.mount.make_directory(self.path, parents=parents, exist_ok=exist_ok)

    def remove(self, *, recursive: bool = False, force: bool = False) -> None:
        self.mount.remove(self.path, recursive=recursive, force=force)


#: Earlier releases addressed only directories through this node.
DirectoryNode = PathNode


# ---------------------------------------------------------------------------
# Filesystem registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Candidate:
    """One identification result."""

    filesystem: str
    confidence: float
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "filesystem": self.filesystem,
            "confidence": round(self.confidence, 3),
            "detail": self.detail,
        }


class AmigaDOSFilesystem:
    """Registry entry for OFS and FFS volumes."""

    name = "amigados"
    label = "AmigaDOS OFS/FFS"

    def open(self, reader: BlockReader, geometry: Geometry | None = None) -> AmigaDOSMount:
        return AmigaDOSMount(AmigaDOSVolume(reader, geometry), self.name)

    def identify(self, reader: BlockReader) -> Candidate | None:
        if not reader.total_blocks:
            return None
        signature = reader.read_block(0)[:4]
        if signature[:3] != b"DOS":
            return None
        label = DOS_TYPES.get(signature)
        if label is None:
            return None
        try:
            volume = AmigaDOSVolume(reader)
        except DataError:
            return Candidate(self.name, 0.5, f"{label} boot block without a readable root")
        return Candidate(self.name, 1.0, f"{volume.format} volume named {volume.title!r}")


class OFSFilesystem(AmigaDOSFilesystem):
    name = "ofs"
    label = "AmigaDOS Old File System"

    def identify(self, reader: BlockReader) -> Candidate | None:
        found = super().identify(reader)
        if found is None:
            return None
        signature = reader.read_block(0)[:4]
        if signature[3] & 1:
            return None
        return Candidate(self.name, found.confidence, found.detail)


class FFSFilesystem(AmigaDOSFilesystem):
    name = "ffs"
    label = "AmigaDOS Fast File System"

    def identify(self, reader: BlockReader) -> Candidate | None:
        found = super().identify(reader)
        if found is None:
            return None
        signature = reader.read_block(0)[:4]
        if not signature[3] & 1:
            return None
        return Candidate(self.name, found.confidence, found.detail)


class SFSFilesystem:
    """Registry entry for Smart File System volumes."""

    name = "sfs"
    label = "Smart File System"

    def open(self, reader: BlockReader, geometry: Geometry | None = None) -> SFSMount:
        return SFSMount(SFSVolume(reader), self.name)

    def identify(self, reader: BlockReader) -> Candidate | None:
        if not reader.total_blocks or reader.read_block(0)[:4] != SFS_ID:
            return None
        try:
            volume = SFSVolume(reader)
        except DataError:
            return Candidate(self.name, 0.5, "SFS root block that does not validate")
        try:
            return Candidate(self.name, 1.0, f"SFS volume named {volume.title!r}")
        finally:
            volume.blocks.close()


class PFS3Filesystem:
    """Registry entry for Professional File System volumes."""

    name = "pfs3"
    label = "Professional File System 3"

    def open(self, reader: BlockReader, geometry: Geometry | None = None) -> PFS3Mount:
        return PFS3Mount(PFS3Volume(reader), self.name)

    def identify(self, reader: BlockReader) -> Candidate | None:
        if not reader.total_blocks or reader.read_block(0)[:4] not in PFS3_ROOT_IDS:
            return None
        try:
            volume = PFS3Volume(reader)
        except DataError:
            return Candidate(self.name, 0.5, "PFS3 boot block without a readable root")
        try:
            return Candidate(self.name, 1.0, f"PFS3 volume named {volume.title!r}")
        finally:
            volume.blocks.close()


class RigidDiskFilesystem:
    """Registry entry for a partitioned hard-drive file."""

    name = "rdb"
    label = "Rigid Disk Block hard drive"

    def open(self, reader: BlockReader, geometry: Geometry | None = None) -> "RigidDiskMount":
        return RigidDiskMount(reader)

    def identify(self, reader: BlockReader) -> Candidate | None:
        block = find_rdb_block(reader)
        if block is None:
            return None
        try:
            disk = read_rigid_disk(reader)
        except DataError as error:
            return Candidate(self.name, 0.5, str(error))
        return Candidate(
            self.name,
            1.0,
            f"{len(disk.partitions)} partition(s) on {disk.cylinders} cylinders",
        )


class KickstartFilesystem:
    """Registry entry for a Kickstart ROM's resident-module list."""

    name = "kickfs"
    label = "Kickstart ROM modules"

    def open(self, reader: BlockReader, geometry: Geometry | None = None):
        from ..kickfs.kickfs import KickstartMount

        return KickstartMount(reader)

    #: A ROM has to fit the 68000's 24-bit address space, so anything larger is
    #: not one. Checking first matters: the probe reads the whole input, and a
    #: hard drive opened in place would otherwise be read into memory entire.
    LARGEST_ROM = 0x1000000

    def identify(self, reader: BlockReader) -> Candidate | None:
        from ..kickfs.kickfs import KICKFS

        if reader.length > self.LARGEST_ROM:
            return None
        try:
            image = KICKFS.from_bytes(_whole_image(reader))
        except DataError:
            return None
        return Candidate(
            self.name,
            1.0,
            f"Kickstart {image.version} with {len(image.data_files)} module(s)",
        )


class RigidDiskMount:
    """A partitioned drive presented as a list of mountable volumes."""

    def __init__(self, reader: BlockReader):
        self.reader = reader
        self.disk: RigidDisk = read_rigid_disk(reader)
        self.filesystem = "rdb"

    @property
    def partitions(self) -> list[Partition]:
        return self.disk.partitions

    def partition(self, index: int) -> Partition:
        for candidate in self.disk.partitions:
            if candidate.index == index:
                return candidate
        raise DataError(f"Partition {index} does not exist on this drive.")

    def open_partition(self, index: int, *, writable: bool | None = None) -> AmigaDOSMount:
        partition = self.partition(index)
        if writable is None:
            writable = self.reader.writable
        # A read-only window must not hold a writable handle, so the window is
        # opened with the access it will keep rather than adjusted afterwards.
        window = self.reader.reopen(
            writable=bool(writable) and self.reader.writable,
            offset=self.reader.offset + partition.start_block * self.reader.block_size,
            length=partition.total_blocks * self.reader.block_size,
        )
        if partition.dos_type == SFS_ID:
            return SFSMount(SFSVolume(window))
        if partition.dos_type in PFS3_DOS_TYPES:
            return PFS3Mount(PFS3Volume(window))
        return AmigaDOSMount(AmigaDOSVolume(window, partition.geometry()))

    def to_dict(self) -> dict:
        return self.disk.to_dict()

    def close(self) -> None:
        self.reader.close()


FILESYSTEMS = {
    "amigados": AmigaDOSFilesystem,
    "ofs": OFSFilesystem,
    "ffs": FFSFilesystem,
    "rdb": RigidDiskFilesystem,
    "sfs": SFSFilesystem,
    "pfs3": PFS3Filesystem,
    "kickfs": KickstartFilesystem,
}

#: Identification order. The partition table is checked first because it wraps
#: volumes that would otherwise be found at an offset.
IDENTIFY_ORDER = ("rdb", "ffs", "ofs", "sfs", "pfs3", "kickfs")


def create_filesystem(name: str):
    """Return a filesystem driver by name."""
    key = str(name or "").strip().lower()
    if key not in FILESYSTEMS:
        raise ConfigurationError(f"{name!r} is not a filing system this build provides.")
    return FILESYSTEMS[key]()


def list_filesystems() -> list[dict]:
    seen: dict[str, dict] = {}
    for key, factory in FILESYSTEMS.items():
        driver = factory()
        seen[key] = {"name": driver.name, "label": driver.label}
    return list(seen.values())


def _whole_image(reader: BlockReader) -> bytes:
    return reader.read_all()


SUFFIX_HINTS = {
    ".adf": ("ofs", "ffs"),
    ".adz": ("ofs", "ffs"),
    ".dsk": ("ofs", "ffs"),
    ".hdf": ("rdb", "ffs", "ofs", "sfs", "pfs3"),
    ".hda": ("ffs", "ofs", "rdb", "sfs", "pfs3"),
    ".hdz": ("rdb", "ffs", "ofs", "sfs", "pfs3"),
    ".rdsk": ("rdb",),
    ".img": ("rdb", "ffs", "ofs", "sfs", "pfs3"),
    ".raw": ("rdb", "ffs", "ofs", "sfs", "pfs3"),
    ".rom": ("kickfs",),
    ".kick": ("kickfs",),
}


def identify(
    path: Path | str,
    *,
    suffix_hint: str | None = None,
    filesystems: dict | None = None,
) -> list[Candidate]:
    """Identify an image by content, best guess first.

    ``suffix_hint`` only reorders the cascade; it never lets a filing system
    claim bytes it cannot actually read. ``filesystems`` restricts the cascade
    to a known set, which is how the workbench avoids scanning a whole hard
    drive for a Kickstart ROM it already knows is not there.
    """
    path = Path(path)
    drivers = filesystems if filesystems is not None else {
        name: create_filesystem(name) for name in IDENTIFY_ORDER
    }
    order = list(drivers)
    hint = (suffix_hint or path.suffix or "").lower()
    preferred = SUFFIX_HINTS.get(hint, ())
    order.sort(key=lambda name: preferred.index(name) if name in preferred else len(preferred))
    results: list[Candidate] = []
    for name in order:
        driver = drivers[name]
        reader = None
        try:
            reader = reader_for(path)
            found = driver.identify(reader)
        except (DataError, OSError):
            found = None
        finally:
            if reader is not None:
                reader.close()
        if found is not None:
            results.append(found)
    results.sort(key=lambda candidate: candidate.confidence, reverse=True)
    return results


def identify_json(path: Path | str, *, suffix_hint: str | None = None) -> str:
    rows = [candidate.to_dict() for candidate in identify(path, suffix_hint=suffix_hint)]
    return json.dumps({"reports": {"candidates": {"rows": rows}}})


__all__ = [
    "AmigaDOSFilesystem",
    "AmigaDOSMount",
    "AmigaMetadata",
    "DirectoryNode",
    "PathNode",
    "BLOCK_SIZE",
    "Candidate",
    "DD_BLOCKS",
    "Datestamped",
    "Entry",
    "FFSFilesystem",
    "FILESYSTEMS",
    "FileSystemHandler",
    "Filetyped",
    "Geometry",
    "HD_BLOCKS",
    "KickstartFilesystem",
    "NAMED_GEOMETRIES",
    "OFSFilesystem",
    "PFS3Filesystem",
    "PFS3Mount",
    "Partition",
    "RigidDisk",
    "RigidDiskFilesystem",
    "RigidDiskMount",
    "SFSFilesystem",
    "SFSMount",
    "Stat",
    "create_filesystem",
    "format_volume",
    "geometry_from_dsc",
    "geometry_from_geo",
    "identify",
    "identify_json",
    "join_path",
    "list_filesystems",
    "partition_reader",
    "read_rigid_disk",
    "reader_for",
    "split_path",
    "validate_name",
    "write_geometry",
    "write_rigid_disk",
]
