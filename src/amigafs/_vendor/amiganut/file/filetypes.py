"""Amiga file-type recognition.

AmigaDOS has no filetype field in the catalogue. A file's kind comes from its
content and, for anything that appears on the Workbench, from the icon file
that sits beside it with a ``.info`` suffix. This module owns both routes so
the workbench classifies a file the same way whether it is listing a
directory or opening one file.

The numeric codes are the Workbench object types stored in a ``.info`` file,
so a type that came from an icon round-trips exactly.
"""

from __future__ import annotations

import struct

from ..errors import DataError

# Workbench object types, from the DiskObject structure.
WBDISK = 1
WBDRAWER = 2
WBTOOL = 3
WBPROJECT = 4
WBGARBAGE = 5
WBDEVICE = 6
WBKICK = 7
WBAPPICON = 8

TYPE_NAMES = {
    WBDISK: "Disk",
    WBDRAWER: "Drawer",
    WBTOOL: "Tool",
    WBPROJECT: "Project",
    WBGARBAGE: "Trashcan",
    WBDEVICE: "Device",
    WBKICK: "Kickstart",
    WBAPPICON: "AppIcon",
}

NAMED_TYPES = {name.casefold(): code for code, name in TYPE_NAMES.items()}

#: Magic numbers that identify Amiga content without an icon.
HUNK_HEADER = 0x000003F3
HUNK_UNIT = 0x000003E7
HUNK_LIB = 0x000003FB
ILBM_FORM = b"FORM"
AMIGABASIC_TOKEN = b"\xf5\x00"
SCRIPT_SHEBANG = b"/*"


def parse_filetype(value: str | int | None) -> int | None:
    """Accept a Workbench type by number or by name."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        code = value
    else:
        text = str(value).strip()
        if text.casefold() in NAMED_TYPES:
            return NAMED_TYPES[text.casefold()]
        try:
            code = int(text, 0)
        except ValueError:
            raise DataError(
                "Choose a Workbench type: Disk, Drawer, Tool, Project, "
                "Trashcan, Device, Kickstart or AppIcon."
            ) from None
    if code not in TYPE_NAMES:
        raise DataError(f"{code} is not a Workbench object type.")
    return code


def format_filetype(value: int | None) -> str:
    """Render a Workbench type as its familiar name."""
    if value is None:
        return ""
    return TYPE_NAMES.get(int(value), f"Type {int(value)}")


def icon_name(path: str) -> str:
    """Return the icon path that Workbench would pair with this entry."""
    return f"{path}.info"


def icon_type(icon_bytes: bytes) -> int | None:
    """Read the Workbench object type out of a ``.info`` icon file."""
    if len(icon_bytes) < 48:
        return None
    magic, version = struct.unpack_from(">HH", icon_bytes, 0)
    if magic != 0xE310:
        return None
    if version not in (1,):
        return None
    (object_type,) = struct.unpack_from(">H", icon_bytes, 48)
    return object_type if object_type in TYPE_NAMES else None


def detect_content_type(data: bytes) -> str | None:
    """Classify Amiga content from its leading bytes.

    Returns one of ``executable``, ``library``, ``object``, ``iff``,
    ``basic``, ``script`` or ``None`` when nothing is recognised.
    """
    if len(data) >= 4:
        (magic,) = struct.unpack_from(">I", data, 0)
        if magic == HUNK_HEADER:
            return "executable"
        if magic == HUNK_LIB:
            return "library"
        if magic == HUNK_UNIT:
            return "object"
    if data[:4] == ILBM_FORM:
        return "iff"
    if data[:2] == AMIGABASIC_TOKEN:
        return "basic"
    if data[:2] == SCRIPT_SHEBANG or data[:1] == b";":
        return "script"
    return None


__all__ = [
    "TYPE_NAMES",
    "WBAPPICON",
    "WBDEVICE",
    "WBDISK",
    "WBDRAWER",
    "WBGARBAGE",
    "WBKICK",
    "WBPROJECT",
    "WBTOOL",
    "detect_content_type",
    "format_filetype",
    "icon_name",
    "icon_type",
    "minimal_icon",
    "parse_filetype",
]


def minimal_icon(object_type: int) -> bytes:
    """Build the smallest valid ``.info`` file for a Workbench object type.

    Workbench needs a DiskObject header and one 1x1 image plane before it will
    display an icon at all. Producing that here means a type recorded during
    an import is visible on a real machine rather than only inside this
    workbench.
    """
    import struct

    code = parse_filetype(object_type)
    if code is None:
        raise DataError("A Workbench type is required to build an icon.")
    icon = bytearray(78)
    struct.pack_into(">HH", icon, 0, 0xE310, 1)      # magic, version
    struct.pack_into(">I", icon, 4, 0)               # NextGadget
    struct.pack_into(">hhhh", icon, 8, 0, 0, 24, 16)  # LeftEdge, TopEdge, Width, Height
    struct.pack_into(">HHH", icon, 16, 5, 0x0003, 1)  # Flags, Activation, GadgetType
    struct.pack_into(">I", icon, 22, 0x00000001)      # GadgetRender placeholder
    struct.pack_into(">H", icon, 48, code)            # do_Type
    struct.pack_into(">I", icon, 50, 0)               # do_DefaultTool
    struct.pack_into(">I", icon, 54, 0)               # do_ToolTypes
    struct.pack_into(">II", icon, 58, 0x80000000, 0x80000000)  # NO_ICON_POSITION
    struct.pack_into(">I", icon, 66, 0)               # do_DrawerData
    struct.pack_into(">I", icon, 70, 0)               # do_ToolWindow
    struct.pack_into(">I", icon, 74, 4096)            # do_StackSize
    # One 24x16 two-plane image, all background.
    image = bytearray(20 + 2 * 2 * 16)
    struct.pack_into(">hhhh", image, 0, 0, 0, 24, 16)
    struct.pack_into(">h", image, 8, 2)
    struct.pack_into(">I", image, 10, 20)
    image[14] = 0x03
    return bytes(icon) + bytes(image)
