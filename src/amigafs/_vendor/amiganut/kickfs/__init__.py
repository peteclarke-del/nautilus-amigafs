"""Kickstart ROM decoding, presented as a read-only filing system.

A Kickstart ROM is not a disk, but it is a container of named, versioned,
individually addressable parts: the resident modules the ROM tag scan finds at
boot. Presenting that list as a filesystem lets the workbench browse, export
and compare ROM contents with the same tools it uses for a floppy.
"""

from .kickfs import (
    KICKFS,
    Kickstart,
    KickstartMount,
    ResidentModule,
    build_rom,
    rom_checksum,
    set_copyright,
    set_version,
)

__all__ = [
    "KICKFS",
    "Kickstart",
    "KickstartMount",
    "ResidentModule",
    "build_rom",
    "rom_checksum",
    "set_copyright",
    "set_version",
]
