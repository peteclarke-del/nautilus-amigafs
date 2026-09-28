"""Per-file Amiga catalogue metadata.

An AmigaDOS catalogue entry carries three things a workbench needs to
preserve when a file moves between volumes: its protection bits, its free-text
comment and its datestamp. This module owns all three, plus the file-type
recognition used for icons and content classification.

Protection bits are stored in the file header block as one big-endian long.
The low eight bits are, from bit 7 down: H S P A R W E D. The R, W, E and D
bits are *inverted* on disk -- a clear bit means the permission is granted --
which is the single most common source of mistakes when reading Amiga
metadata by hand, so it is handled here once.
"""

from __future__ import annotations

import re
from enum import IntFlag
from datetime import datetime, timedelta, timezone

from ..errors import DataError
from . import filetypes as filetypes  # re-exported for callers

# Bit positions inside the protection long.
FIBF_DELETE = 1 << 0     # inverted: clear = deletable
FIBF_EXECUTE = 1 << 1    # inverted: clear = executable
FIBF_WRITE = 1 << 2      # inverted: clear = writable
FIBF_READ = 1 << 3       # inverted: clear = readable
FIBF_ARCHIVE = 1 << 4
FIBF_PURE = 1 << 5
FIBF_SCRIPT = 1 << 6
FIBF_HOLD = 1 << 7

INVERTED_BITS = FIBF_DELETE | FIBF_EXECUTE | FIBF_WRITE | FIBF_READ

#: Canonical display order, matching ``List`` on a real machine.
FLAG_ORDER = (
    ("h", FIBF_HOLD, False),
    ("s", FIBF_SCRIPT, False),
    ("p", FIBF_PURE, False),
    ("a", FIBF_ARCHIVE, False),
    ("r", FIBF_READ, True),
    ("w", FIBF_WRITE, True),
    ("e", FIBF_EXECUTE, True),
    ("d", FIBF_DELETE, True),
)

#: A newly created file is readable, writable, executable and deletable.
DEFAULT_PROTECTION = 0

# The AmigaDOS epoch. Datestamps count days, minutes and 1/50th-second ticks
# from midnight on 1 January 1978.
AMIGA_EPOCH = datetime(1978, 1, 1, tzinfo=timezone.utc)


class Access(IntFlag):
    """Decoded protection bits for one catalogue entry.

    The four permission bits are stored inverted on disk: a *set* bit removes
    the permission. That inversion is preserved here rather than hidden,
    because the raw long is what a real machine reads and what the workbench
    writes back. The readable helpers below do the interpretation.
    """

    D = FIBF_DELETE
    E = FIBF_EXECUTE
    W = FIBF_WRITE
    R = FIBF_READ
    A = FIBF_ARCHIVE
    P = FIBF_PURE
    S = FIBF_SCRIPT
    H = FIBF_HOLD

    #: Locked: neither writable nor deletable. The workbench's lock control.
    L = FIBF_DELETE | FIBF_WRITE
    #: The execute bit, under the name the workbench's run-only control uses.
    X = FIBF_EXECUTE

    @property
    def readable(self) -> bool:
        return not self & Access.R

    @property
    def writable(self) -> bool:
        return not self & Access.W

    @property
    def executable(self) -> bool:
        return not self & Access.E

    @property
    def deletable(self) -> bool:
        return not self & Access.D

    @property
    def archived(self) -> bool:
        return bool(self & Access.A)

    @property
    def pure(self) -> bool:
        return bool(self & Access.P)

    @property
    def script(self) -> bool:
        return bool(self & Access.S)

    @property
    def hold(self) -> bool:
        return bool(self & Access.H)

    @property
    def locked(self) -> bool:
        """True when the entry cannot be deleted or written."""
        return not self.deletable or not self.writable

    def with_locked(self, locked: bool) -> "Access":
        return (self | Access.L) if locked else Access(self.value & ~Access.L.value)

    def __str__(self) -> str:  # pragma: no cover - convenience
        return format_access_text(self)


def format_access_text(access: Access | int | None) -> str:
    """Render protection bits the way ``List`` does, for example ``----rwed``."""
    if access is None:
        return ""
    value = access.value if isinstance(access, Access) else int(access)
    letters = []
    for letter, mask, inverted in FLAG_ORDER:
        present = (not value & mask) if inverted else bool(value & mask)
        letters.append(letter if present else "-")
    return "".join(letters)


def parse_access_text(text: str) -> Access:
    """Parse ``hsparwed`` style protection text into protection bits."""
    text = str(text or "").strip().lower()
    if not text:
        return Access(DEFAULT_PROTECTION)
    if not re.fullmatch(r"[hsparwed-]{1,8}", text):
        raise DataError(
            "Protection flags may only contain h, s, p, a, r, w, e, d or -."
        )
    if len(text) == 8:
        selected = {
            letter for letter, character in zip("hsparwed", text) if character != "-"
        }
    else:
        selected = {character for character in text if character != "-"}
    value = 0
    for letter, mask, inverted in FLAG_ORDER:
        granted = letter in selected
        if inverted:
            if not granted:
                value |= mask
        elif granted:
            value |= mask
    return Access(value)


def parse_protection_value(text: str | int | None) -> int:
    """Parse a 32-bit protection value written as decimal or hexadecimal.

    ``0x``, ``$`` and ``&`` prefixes are all accepted because Amiga
    documentation, AmigaDOS scripts and assembler sources each use a
    different one.
    """
    if text is None or text == "":
        return 0
    if isinstance(text, int):
        value = text
    else:
        cleaned = str(text).strip().replace("_", "")
        if not cleaned:
            return 0
        match = re.fullmatch(r"(?:0[xX]|\$|&)?([0-9A-Fa-f]{1,8})", cleaned)
        if match and (
            cleaned[0] in "$&"
            or cleaned[:2].lower() == "0x"
            or re.search(r"[A-Fa-f]", cleaned)
        ):
            value = int(match.group(1), 16)
        else:
            try:
                value = int(cleaned, 10)
            except ValueError:
                if match:
                    value = int(match.group(1), 16)
                else:
                    raise DataError(f"{text!r} is not a 32-bit value.") from None
    if not 0 <= value <= 0xFFFFFFFF:
        raise DataError("A 32-bit value must be between 0 and &FFFFFFFF.")
    return value


def format_address(value: int | None) -> str:
    """Render a 32-bit value in the ``&`` hexadecimal form the workbench uses."""
    return f"&{int(value or 0) & 0xFFFFFFFF:08X}"


def datestamp_to_datetime(days: int, mins: int, ticks: int) -> datetime:
    """Convert an AmigaDOS days/minutes/ticks triple into a datetime."""
    return AMIGA_EPOCH + timedelta(
        days=int(days), minutes=int(mins), seconds=int(ticks) / 50.0
    )


def datetime_to_datestamp(moment: datetime) -> tuple[int, int, int]:
    """Convert a datetime into an AmigaDOS days/minutes/ticks triple."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta = moment.astimezone(timezone.utc) - AMIGA_EPOCH
    if delta.total_seconds() < 0:
        raise DataError("Amiga datestamps cannot precede 1 January 1978.")
    days = delta.days
    remainder = delta.seconds + delta.microseconds / 1_000_000
    mins = int(remainder // 60)
    ticks = int(round((remainder - mins * 60) * 50))
    if ticks >= 3000:  # pragma: no cover - rounding guard
        ticks = 2999
    return days, mins, ticks


class AmigaMeta:
    """The catalogue metadata Amiga File Forge preserves across a copy.

    An AmigaDOS entry carries three things worth keeping when a file moves
    between volumes: its protection bits, its free-text comment and its
    datestamp. A Workbench icon type is carried alongside them, because it
    lives in the entry's companion ``.info`` file rather than in the header.

    There is deliberately no load or execution address here. AmigaDOS does not
    record one: a load file carries its own hunk header, and the loader reads
    that. Anything claiming otherwise is describing a different machine.
    """

    __slots__ = ("protection", "comment", "datestamp", "filetype", "extra")

    def __init__(
        self,
        protection: int = DEFAULT_PROTECTION,
        comment: str = "",
        datestamp: datetime | None = None,
        filetype: int | None = None,
        extra: dict | None = None,
        *,
        access: "Access | int | None" = None,
    ):
        if access is not None:
            protection = access.value if isinstance(access, Access) else int(access)
        self.protection = int(protection) & 0xFFFFFFFF
        self.comment = str(comment or "")
        self.datestamp = datestamp
        self.filetype = filetype
        self.extra = dict(extra or {})

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"AmigaMeta(protection=&{self.protection:08X}, comment={self.comment!r}, "
            f"datestamp={self.datestamp!r}, filetype={self.filetype!r})"
        )

    def __eq__(self, other) -> bool:
        if not isinstance(other, AmigaMeta):
            return NotImplemented
        return (
            self.protection == other.protection
            and self.comment == other.comment
            and self.datestamp == other.datestamp
            and self.filetype == other.filetype
        )

    @property
    def access(self) -> Access:
        return Access(self.protection)

    def with_protection(self, value: int) -> "AmigaMeta":
        return AmigaMeta(
            protection=int(value) & 0xFFFFFFFF,
            comment=self.comment,
            datestamp=self.datestamp,
            filetype=self.filetype,
            extra=dict(self.extra),
        )

    def with_comment(self, value: str) -> "AmigaMeta":
        text = str(value or "")
        if len(text) > 79:
            raise DataError("An Amiga file comment can hold at most 79 characters.")
        return AmigaMeta(
            protection=self.protection,
            comment=text,
            datestamp=self.datestamp,
            filetype=self.filetype,
            extra=dict(self.extra),
        )


__all__ = [
    "AMIGA_EPOCH",
    "Access",
    "AmigaMeta",
    "DEFAULT_PROTECTION",
    "FIBF_ARCHIVE",
    "FIBF_DELETE",
    "FIBF_EXECUTE",
    "FIBF_HOLD",
    "FIBF_PURE",
    "FIBF_READ",
    "FIBF_SCRIPT",
    "FIBF_WRITE",
    "FLAG_ORDER",
    "datestamp_to_datetime",
    "datetime_to_datestamp",
    "filetypes",
    "format_access_text",
    "format_address",
    "parse_access_text",
    "parse_protection_value",
]
