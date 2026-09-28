"""Kickstart ROM identity and resident-module decoding.

A Kickstart ROM announces itself twice. The first four bytes are ``$1111`` or
``$1114`` followed by a ``JMP``, which tells the hardware how big the ROM is
and where to start. The last 24 bytes repeat the size, carry the ones-
complement checksum that the ROM overlay logic verifies, and hold the vectors
copied to address zero at reset.

Between those, the ROM is a run of *resident tags*: a ``$4AFC`` match word
followed by a pointer back to itself. That self-reference is what makes the
scan reliable, because it distinguishes a real tag from the same two bytes
appearing inside code, and it is what lets this module map every module in the
ROM to a name, a version and an exact byte range.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ..errors import ConfigurationError, DataError

RTC_MATCHWORD = 0x4AFC
RESIDENT_SIZE = 0x1A

HEADER_256K = 0x1111
HEADER_512K = 0x1114

SIZE_256K = 256 * 1024
SIZE_512K = 512 * 1024
VALID_SIZES = (SIZE_256K, SIZE_512K, 2 * SIZE_512K)

#: Where the ROM appears in the 68000 address space, by size.
ROM_BASES = {
    SIZE_256K: 0xFC0000,
    SIZE_512K: 0xF80000,
    2 * SIZE_512K: 0xF00000,
}

NODE_TYPES = {
    0: "unknown",
    1: "task",
    2: "interrupt",
    3: "device",
    4: "msgport",
    5: "message",
    6: "freemsg",
    7: "replymsg",
    8: "resource",
    9: "library",
    10: "memory",
    11: "softint",
    12: "font",
    13: "process",
    14: "semaphore",
    15: "signalsem",
    16: "bootnode",
    17: "kickmem",
    18: "graphics",
    19: "deathmessage",
}

RTF_AUTOINIT = 1 << 7
RTF_AFTERDOS = 1 << 2
RTF_SINGLETASK = 1 << 1
RTF_COLDSTART = 1 << 0


@dataclass(frozen=True)
class ResidentModule:
    """One resident tag decoded into a browsable entry."""

    name: str
    id_string: str
    version: int
    revision: int
    node_type: int
    priority: int
    flags: int
    tag_offset: int
    start: int
    end: int
    data: bytes = field(repr=False, default=b"")

    @property
    def length(self) -> int:
        return max(0, self.end - self.start)

    @property
    def type_name(self) -> str:
        return NODE_TYPES.get(self.node_type, f"type {self.node_type}")

    @property
    def autoinit(self) -> bool:
        return bool(self.flags & RTF_AUTOINIT)

    @property
    def blocks(self) -> int:
        return max(1, -(-self.length // 512))

    @property
    def complete(self) -> bool:
        return self.length > 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "idString": self.id_string,
            "version": self.version,
            "revision": self.revision,
            "type": self.type_name,
            "priority": self.priority,
            "autoinit": self.autoinit,
            "offset": self.start,
            "length": self.length,
        }


def _read_cstring(data: bytes, offset: int, limit: int = 200) -> str:
    if not 0 <= offset < len(data):
        return ""
    end = data.find(b"\0", offset, min(len(data), offset + limit))
    if end < 0:
        end = min(len(data), offset + limit)
    return data[offset:end].decode("latin-1", "replace").strip()


def rom_checksum(data: bytes, *, skip_offset: int | None = None) -> int:
    """Return the ones-complement checksum Kickstart verifies at reset."""
    total = 0
    for offset in range(0, len(data) - 3, 4):
        if skip_offset is not None and offset == skip_offset:
            continue
        (value,) = struct.unpack_from(">I", data, offset)
        total += value
        if total > 0xFFFFFFFF:
            total = (total & 0xFFFFFFFF) + 1
    return (~total) & 0xFFFFFFFF


class Kickstart:
    """A decoded Kickstart ROM."""

    def __init__(self, data: bytes):
        self.data = bytes(data)
        size = len(self.data)
        if size < 1024:
            raise DataError("A Kickstart ROM is at least 1 KiB.")
        (self.header,) = struct.unpack_from(">H", self.data, 0)
        self.expected_size = {
            HEADER_256K: SIZE_256K,
            HEADER_512K: SIZE_512K,
        }.get(self.header)
        if self.expected_size is None:
            raise DataError(
                "The image does not begin with a Kickstart ROM header "
                f"(&{self.header:04X} is not &1111 or &1114)."
            )
        self.base = ROM_BASES.get(size, 0x1000000 - size)
        self.checksum_offset = size - 24
        self.stored_checksum = (
            struct.unpack_from(">I", self.data, self.checksum_offset)[0]
            if size >= 24
            else 0
        )
        self.declared_size = (
            struct.unpack_from(">I", self.data, size - 20)[0] if size >= 20 else 0
        )
        (self.rom_version, self.rom_revision) = struct.unpack_from(">HH", self.data, 12)
        self.modules = self._scan_modules()

    # ---- identity -----------------------------------------------------
    @property
    def version(self) -> str:
        return f"{self.rom_version}.{self.rom_revision}"

    @property
    def release(self) -> str:
        """The familiar AmigaOS release name for this exec version."""
        return {
            30: "Kickstart 1.1",
            31: "Kickstart 1.1",
            32: "Kickstart 1.2",
            33: "Kickstart 1.2",
            34: "Kickstart 1.3",
            36: "Kickstart 2.0",
            37: "Kickstart 2.04",
            38: "Kickstart 2.1",
            39: "Kickstart 3.0",
            40: "Kickstart 3.1",
            45: "Kickstart 3.1.4",
            46: "Kickstart 3.2",
        }.get(self.rom_version, f"exec {self.version}")

    @property
    def primary_module(self) -> "ResidentModule | None":
        """The module a ROM is identified by.

        On a Kickstart that is ``exec.library``, which is always its
        highest-priority module. An expansion ROM has no exec at all, so its
        first resident tag stands in: on both, that is the module whose
        identification string the ROM scan reports.
        """
        return self.module("exec.library") or (self.modules[0] if self.modules else None)

    @property
    def title(self) -> str:
        module = self.primary_module
        return module.name if module is not None else self.release

    @property
    def header_title(self) -> str:
        return self.release

    @property
    def copyright(self) -> str:
        module = self.primary_module
        return module.id_string if module is not None else ""

    @property
    def rom_type(self) -> str:
        return f"{len(self.data) // 1024} KiB Kickstart"

    @property
    def data_offset(self) -> int:
        return self.modules[0].start if self.modules else 0

    @property
    def data_files(self) -> list[ResidentModule]:
        return self.modules

    # ---- integrity ----------------------------------------------------
    @property
    def checksum_valid(self) -> bool:
        return rom_checksum(self.data, skip_offset=self.checksum_offset) == self.stored_checksum

    @property
    def size_valid(self) -> bool:
        return len(self.data) == self.expected_size or len(self.data) in VALID_SIZES

    @property
    def is_complete(self) -> bool:
        """True when the header, declared size and checksum all agree."""
        return bool(
            self.size_valid
            and self.modules
            and self.declared_size in (len(self.data), 0)
        )

    @property
    def is_plain(self) -> bool:
        """False for an encrypted or split ROM that cannot be rebuilt safely."""
        if not self.modules:
            return False
        if self.data[:11] == b"AMIROMTYPE1":
            return False
        return self.checksum_valid

    @property
    def encrypted(self) -> bool:
        return self.data[:11] == b"AMIROMTYPE1"

    @property
    def _fs_end(self) -> int:
        return max((module.end for module in self.modules), default=len(self.data))

    # ---- modules ------------------------------------------------------
    def _scan_modules(self) -> list[ResidentModule]:
        data = self.data
        found: list[ResidentModule] = []
        offset = 0
        limit = len(data) - RESIDENT_SIZE
        while offset <= limit:
            index = data.find(b"\x4a\xfc", offset)
            if index < 0 or index > limit:
                break
            offset = index + 2
            if index % 2:
                continue
            (match_tag,) = struct.unpack_from(">I", data, index + 2)
            if match_tag != self.base + index:
                continue
            (end_skip,) = struct.unpack_from(">I", data, index + 6)
            flags = data[index + 10]
            version = data[index + 11]
            node_type = data[index + 12]
            priority = struct.unpack_from(">b", data, index + 13)[0]
            (name_ptr,) = struct.unpack_from(">I", data, index + 14)
            (id_ptr,) = struct.unpack_from(">I", data, index + 18)
            name = _read_cstring(data, name_ptr - self.base) if name_ptr else ""
            id_string = _read_cstring(data, id_ptr - self.base) if id_ptr else ""
            if not name:
                continue
            end = end_skip - self.base
            if not 0 < end <= len(data):
                end = min(len(data), index + RESIDENT_SIZE)
            revision = 0
            match = id_string.split()
            if len(match) > 1 and "." in match[1]:
                head, _, tail = match[1].partition(".")
                if head.isdigit() and tail.isdigit():
                    revision = int(tail)
            found.append(
                ResidentModule(
                    name=name,
                    id_string=id_string,
                    version=version,
                    revision=revision,
                    node_type=node_type,
                    priority=priority,
                    flags=flags,
                    tag_offset=index,
                    start=index,
                    end=end,
                )
            )
        found.sort(key=lambda module: (-module.priority, module.name.casefold()))
        return found

    def module(self, name: str) -> ResidentModule | None:
        target = str(name).casefold()
        for candidate in self.modules:
            if candidate.name.casefold() == target:
                return candidate
        return None

    def read_module(self, name: str) -> bytes:
        module = self.module(name)
        if module is None:
            raise DataError(f"{name} is not a resident module in this ROM.")
        return self.data[module.start : module.end]

    # ---- constructors -------------------------------------------------
    @classmethod
    def from_bytes(cls, data: bytes) -> "Kickstart":
        return cls(data)

    def to_dict(self) -> dict:
        return {
            "release": self.release,
            "version": self.version,
            "size": len(self.data),
            "base": self.base,
            "checksumValid": self.checksum_valid,
            "declaredSize": self.declared_size,
            "encrypted": self.encrypted,
            "modules": [module.to_dict() for module in self.modules],
        }


#: The name Amiga File Forge uses when it asks for the Kickstart filesystem.
KICKFS = Kickstart


def set_version(data: bytes, version: int) -> bytes:
    """Rewrite the exec version word and repair the ROM checksum."""
    version = int(version)
    if not 0 <= version <= 0xFFFF:
        raise ConfigurationError("A Kickstart version must be from 0 to 65535.")
    updated = bytearray(data)
    struct.pack_into(">H", updated, 12, version)
    return _reseal(updated)


def set_copyright(data: bytes, text: str) -> bytes:
    """Replace the ROM's identification string in place.

    The string is written back into the same bytes it occupied so no pointer
    inside the ROM moves. A longer string is refused rather than truncated,
    because a silently shortened identification string is indistinguishable
    from a corrupted ROM.
    """
    rom = Kickstart(data)
    # A Kickstart is labelled through exec.library, which is always its
    # highest-priority module. An expansion ROM has no exec at all, so the
    # first resident tag stands in: on both, that is the module whose
    # identification string the ROM scan reports.
    exec_module = rom.module("exec.library") or (rom.modules[0] if rom.modules else None)
    if exec_module is None:
        raise DataError("This ROM has no resident tag to label.")
    (id_ptr,) = struct.unpack_from(">I", rom.data, exec_module.tag_offset + 18)
    offset = id_ptr - rom.base
    if not 0 <= offset < len(rom.data):
        raise DataError("The exec.library identification string is outside the ROM.")
    end = rom.data.find(b"\0", offset)
    if end < 0:
        raise DataError("The exec.library identification string is not terminated.")
    available = end - offset
    encoded = str(text or "").encode("latin-1", "replace")
    if len(encoded) > available:
        raise ConfigurationError(
            f"The identification string can hold at most {available} characters "
            "without moving anything else in the ROM."
        )
    updated = bytearray(rom.data)
    updated[offset : offset + available] = encoded.ljust(available, b"\0")
    return _reseal(updated)


def _reseal(updated: bytearray) -> bytes:
    checksum_offset = len(updated) - 24
    if checksum_offset > 0:
        struct.pack_into(">I", updated, checksum_offset, 0)
        struct.pack_into(
            ">I",
            updated,
            checksum_offset,
            rom_checksum(bytes(updated), skip_offset=checksum_offset),
        )
    return bytes(updated)


class KickstartMount:
    """A Kickstart ROM presented as a read-only directory of modules."""

    def __init__(self, reader):
        self.reader = reader
        self.rom = Kickstart(reader.read_all())
        self.filesystem = "kickfs"
        self.read_only = True

    @property
    def title(self) -> str:
        return self.rom.title

    def set_title(self, value: str) -> None:
        raise DataError("A Kickstart ROM's identity is fixed by its resident tags.")

    def exists(self, path: str | None) -> bool:
        if not path or path in {"", ":", "$", "/"}:
            return True
        return self.rom.module(str(path).strip("/:$")) is not None

    def stat(self, path: str | None):
        from ..filesystem.amigados import Stat

        name = str(path or "").strip("/:$")
        if not name:
            return Stat(self.rom.title, "", True, 0, 1, 0, 1)
        module = self.rom.module(name)
        if module is None:
            raise DataError(f"Path not found: {name}")
        return Stat(module.name, module.name, False, module.length, module.blocks, 0, -3)

    def iter_entries(self, path: str | None = None):
        from ..filesystem.amigados import Entry

        if path and str(path).strip("/:$"):
            raise DataError("A Kickstart ROM has one flat module list.")
        for module in self.rom.modules:
            yield Entry(
                name=module.name,
                path=module.name,
                is_dir=False,
                length=module.length,
                block=module.start,
                secondary_type=-3,
            )

    def read_bytes(self, path: str) -> bytes:
        return self.rom.read_module(str(path).strip("/:$"))

    def amiga_meta(self, path: str):
        """Present a module's ROM attributes through the catalogue interface.

        A resident module has no protection bits, but it does have the two
        things the workbench shows in their place: whether the ROM scan
        auto-initialises it, and its identification string. Mapping those onto
        the standard record means the pane, the manifest and a comparison all
        read a module the same way they read a file.
        """
        from ..file import Access, AmigaMeta

        module = self.rom.module(str(path).strip("/:$"))
        if module is None:
            raise DataError(f"Path not found: {path}")
        protection = int(Access.W | Access.D)
        if module.autoinit:
            protection |= int(Access.E)
        return AmigaMeta(
            protection=protection,
            comment=module.id_string,
            datestamp=None,
            filetype=None,
            extra={
                "version": module.version,
                "priority": module.priority,
                "nodeType": module.type_name,
                "offset": module.start,
            },
        )

    def datestamp(self, path: str):
        """A ROM carries no per-module datestamp."""
        return None

    def filetype(self, path: str):
        """Report the Workbench type a ROM module corresponds to, if any."""
        from ..file.filetypes import WBKICK

        return WBKICK

    def size_bytes(self) -> int:
        return len(self.rom.data)

    def free_bytes(self) -> int:
        return max(0, len(self.rom.data) - self.rom._fs_end)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.rom.size_valid:
            problems.append(
                f"The ROM is {len(self.rom.data):,} bytes, which is not a Kickstart size."
            )
        if not self.rom.checksum_valid:
            problems.append("The ROM checksum does not match its contents.")
        if self.rom.encrypted:
            problems.append(
                "This is an encrypted Cloanto ROM. It needs its keyfile before it can be read."
            )
        if not self.rom.modules:
            problems.append("No resident module tags were found.")
        return problems

    def close(self) -> None:
        self.reader.close()


__all__ = [
    "KICKFS",
    "Kickstart",
    "KickstartMount",
    "RESIDENT_SIZE",
    "RTC_MATCHWORD",
    "ResidentModule",
    "build_rom",
    "rom_checksum",
    "set_copyright",
    "set_version",
]


def build_rom(
    *,
    size: int = SIZE_256K,
    name: str = "forge.library",
    id_string: str = "forge.library 1.0 (2026)",
    version: int = 1,
    revision: int = 0,
    node_type: int = 9,
    priority: int = 0,
) -> bytes:
    """Build a valid, empty expansion or Kickstart ROM around one resident tag.

    This is what an autoboot expansion ROM looks like before its driver code
    is linked in: the size header, a jump to the entry point, one resident tag
    with a name and identification string, the size field and the checksum.
    Everything a real machine's ROM scan needs is present, so the result can
    be opened, inspected and programmed exactly like a ROM that came off a
    board.
    """
    if size not in VALID_SIZES:
        raise ConfigurationError(
            "A ROM image is 256 KiB, 512 KiB or 1 MiB."
        )
    header = HEADER_512K if size >= SIZE_512K else HEADER_256K
    base = ROM_BASES.get(size, 0x1000000 - size)
    name_bytes = str(name).encode("latin-1", "replace")[:60] + b"\0"
    id_bytes = str(id_string).encode("latin-1", "replace")[:120] + b"\0"

    rom = bytearray(size)
    tag_offset = 0x0100
    name_offset = 0x0200
    id_offset = 0x0240
    init_offset = 0x0300

    struct.pack_into(">H", rom, 0, header)
    struct.pack_into(">H", rom, 2, 0x4EF9)          # JMP absolute.l
    struct.pack_into(">I", rom, 4, base + init_offset)
    struct.pack_into(">HH", rom, 12, int(version), int(revision))

    struct.pack_into(">H", rom, tag_offset, RTC_MATCHWORD)
    struct.pack_into(">I", rom, tag_offset + 2, base + tag_offset)
    struct.pack_into(">I", rom, tag_offset + 6, base + init_offset + 4)
    rom[tag_offset + 10] = RTF_COLDSTART
    rom[tag_offset + 11] = int(version) & 0xFF
    rom[tag_offset + 12] = int(node_type) & 0xFF
    struct.pack_into(">b", rom, tag_offset + 13, int(priority))
    struct.pack_into(">I", rom, tag_offset + 14, base + name_offset)
    struct.pack_into(">I", rom, tag_offset + 18, base + id_offset)
    struct.pack_into(">I", rom, tag_offset + 22, base + init_offset)

    rom[name_offset : name_offset + len(name_bytes)] = name_bytes
    rom[id_offset : id_offset + len(id_bytes)] = id_bytes
    # MOVEQ #0,D0 / RTS: a valid init routine that reports "nothing to do".
    struct.pack_into(">HH", rom, init_offset, 0x7000, 0x4E75)

    struct.pack_into(">I", rom, size - 20, size)
    return _reseal(rom)
