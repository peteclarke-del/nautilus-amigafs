"""Line-number references inside a tokenised AmigaBASIC program.

``GOTO 1000`` does not store ``1000`` as digits. It stores a marker byte and a
three-byte encoding, so the interpreter can jump without parsing text and so a
renumber can rewrite the destination without changing the line's length. Every
encoded byte keeps its top two bits set to ``01``, which puts them outside the
token range and outside the ASCII control range, making a partially damaged
program easy to recognise rather than easy to misread.
"""

from __future__ import annotations

from ..errors import DataError

#: Marker that introduces an encoded line-number reference.
LINE_NUMBER_TOKEN = 0x8D

#: The largest line number AmigaBASIC accepts.
MAX_LINE_NUMBER = 32767

_ENCODED_LENGTH = 3


def encode_line_number(number: int) -> bytes:
    """Encode a line number as the three bytes that follow the marker."""
    value = int(number)
    if not 0 <= value <= MAX_LINE_NUMBER:
        raise DataError(f"A line number must be from 0 to {MAX_LINE_NUMBER}.")
    return bytes(
        (
            0x40 | ((value >> 12) & 0x3F),
            0x40 | ((value >> 6) & 0x3F),
            0x40 | (value & 0x3F),
        )
    )


def decode_line_number(data: bytes) -> int:
    """Decode the three bytes that follow a line-number marker."""
    if len(data) < _ENCODED_LENGTH:
        raise DataError("A line-number reference is truncated.")
    if any(byte & 0xC0 != 0x40 for byte in data[:_ENCODED_LENGTH]):
        raise DataError("A line-number reference contains bytes outside its encoding.")
    return (
        ((data[0] & 0x3F) << 12) | ((data[1] & 0x3F) << 6) | (data[2] & 0x3F)
    )


__all__ = [
    "LINE_NUMBER_TOKEN",
    "MAX_LINE_NUMBER",
    "decode_line_number",
    "encode_line_number",
]
