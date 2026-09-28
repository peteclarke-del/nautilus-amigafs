"""AmigaBASIC tokenising, detokenising and recognition.

AmigaBASIC stores a program as a run of numbered lines, each one a length
byte, a big-endian line number, a token stream and a terminator. Keywords
become single bytes from ``&80`` upward; the 1.2 release adds a second bank
behind an escape byte rather than renumbering the first, which is why a 1.0
program still runs unchanged under 1.2.

Three things are true of this module and are worth stating, because they are
what the workbench relies on:

* Tokenising and detokenising round-trip exactly. The editor proves it on
  every save rather than trusting it.
* Scanning never allocates the whole program as text, so a listing view can
  be built for a file too large to edit.
* A line's length byte is not touched by a renumber, because the encoding of
  a line-number reference is fixed width.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from ..errors import DataError
from .linenumber import (
    LINE_NUMBER_TOKEN,
    MAX_LINE_NUMBER,
    decode_line_number,
    encode_line_number,
)

#: Every tokenised AmigaBASIC file begins with this byte.
BASIC_MAGIC = 0xF5

#: Escape byte for keywords that did not fit the single-byte bank. Both
#: releases understand it.
ESCAPE_BYTE = 0xFE

#: Escape byte for the keywords AmigaBASIC 1.2 added. A program that uses it
#: will not load under 1.0, which is exactly what the dialect check looks for.
EXTENDED_ESCAPE_BYTE = 0xFF

MAX_LINE_BYTES = 255


class TokenKind(Enum):
    KEYWORD = "keyword"
    LINENUM = "linenum"
    NUMBER = "number"
    STRING = "string"
    IDENT = "ident"
    SYMBOL = "symbol"
    REM = "rem"


class Verdict(Enum):
    BASIC = "basic"
    BASIC_TRAILING = "basic-trailing"
    NOT_BASIC = "not-basic"
    DAMAGED = "damaged"


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    token: int
    value: object
    start: int
    end: int
    text: str = ""


@dataclass(frozen=True)
class Line:
    line_number: int
    start: int
    end: int
    tokens: list = field(default_factory=list)


@dataclass(frozen=True)
class Detection:
    verdict: Verdict
    program_length: int | None = None
    detail: str = ""


# ---------------------------------------------------------------------------
# Dialects
# ---------------------------------------------------------------------------
#: The AmigaBASIC 1.0 keyword bank, in token order from &80.
BASE_KEYWORDS = [
    "END", "FOR", "NEXT", "DATA", "INPUT", "DIM", "READ", "LET",
    "GOTO", "RUN", "IF", "RESTORE", "GOSUB", "RETURN", "REM", "STOP",
    "PRINT", "CLEAR", "LIST", "NEW", "ON", "WAIT", "DEF", "POKE",
    "CONT", "OUT", "LPRINT", "LLIST", "WIDTH", "ELSE", "TRON", "TROFF",
    "SWAP", "ERASE", "EDIT", "ERROR", "RESUME", "DELETE", "AUTO", "RENUM",
    "DEFSTR", "DEFINT", "DEFSNG", "DEFDBL", "LINE", "WHILE", "WEND", "CALL",
    "WRITE", "OPTION", "RANDOMIZE", "OPEN", "CLOSE", "LOAD", "MERGE", "SAVE",
    "COLOR", "CLS", "MOTOR", "BSAVE", "BLOAD", "SOUND", "BEEP", "PSET",
    "PRESET", "SCREEN", "KEY", "LOCATE", "TO", "THEN", "TAB", "STEP",
    "USR", "FN", "SPC", "NOT", "ERL", "ERR", "STRING$", "USING",
    "INSTR", "'", "VARPTR", "CSRLIN", "POINT", "OFF", "INKEY$", "CHAIN",
    "COMMON", "SHARED", "SUB", "STATIC", "LIBRARY", "DECLARE", "WINDOW", "MENU",
    "MOUSE", "OBJECT", "AREA", "AREAFILL", "PATTERN", "PALETTE", "SCROLL", "SAY",
    "TRANSLATE$", "WAVE", "TIMER", "COLLISION", "SLEEP", "STICK", "STRIG", "PTAB",
]

#: AmigaBASIC 1.2 additions, reached through the escape byte.
EXTENDED_KEYWORDS = [
    "UCASE$", "LBOUND", "UBOUND", "SADD", "FRE", "LPOS", "POS", "LOC",
    "LOF", "EOF", "CVI", "CVS", "CVD", "CVL", "MKI$", "MKS$",
    "MKD$", "MKL$", "FIELD", "LSET", "RSET", "GET", "PUT", "RESET",
    "FILES", "NAME", "KILL", "CHDIR", "SYSTEM", "DATE$", "TIME$", "CIRCLE",
    "PAINT", "SEGMENT",
]

#: Operators and functions that tokenise as ordinary infix words.
OPERATOR_KEYWORDS = [
    "AND", "OR", "XOR", "EQV", "IMP", "MOD",
]

FUNCTION_KEYWORDS = [
    "ABS", "ASC", "ATN", "CDBL", "CHR$", "CINT", "CLNG", "COS", "CSNG",
    "EXP", "FIX", "HEX$", "INPUT$", "INT", "LEFT$", "LEN", "LOG", "MID$",
    "OCT$", "PEEK", "RIGHT$", "RND", "SGN", "SIN", "SPACE$", "SQR", "STR$",
    "TAN", "VAL",
]


class Dialect:
    """One AmigaBASIC keyword bank and the escape banks behind it.

    Keywords are packed into the single-byte range first, most-used first, so
    ordinary programs tokenise densely. Anything that does not fit moves to
    the ``&FE`` bank, which both releases understand. The ``&FF`` bank holds
    the keywords 1.2 introduced, so its presence in a program is a definite
    statement that 1.0 cannot run it.
    """

    #: Single-byte keyword codes run from &80 up to, but not including, &FE.
    BASE_CAPACITY = ESCAPE_BYTE - 0x80

    def __init__(self, name: str, ordered: list[str], extended: list[str] | None = None):
        self.name = name
        self.tokens: dict[int, str] = {}
        self.keywords: dict[str, bytes] = {}
        self.escape: dict[int, dict[int, str]] = {}

        base = ordered[: self.BASE_CAPACITY]
        overflow = ordered[self.BASE_CAPACITY :]
        for offset, word in enumerate(base):
            code = 0x80 + offset
            self.tokens[code] = word
            self.keywords[word] = bytes((code,))
        if overflow:
            bank: dict[int, str] = {}
            for offset, word in enumerate(overflow):
                if offset > 0xFF - 0x80:
                    raise DataError("The overflow keyword bank is full.")
                code = 0x80 + offset
                bank[code] = word
                self.keywords[word] = bytes((ESCAPE_BYTE, code))
            self.escape[ESCAPE_BYTE] = bank
        if extended:
            bank = {}
            for offset, word in enumerate(extended):
                code = 0x80 + offset
                bank[code] = word
                self.keywords[word] = bytes((EXTENDED_ESCAPE_BYTE, code))
            self.escape[EXTENDED_ESCAPE_BYTE] = bank

        # Longest first, so MID$ is matched before MID.
        self._pattern = re.compile(
            "|".join(
                re.escape(word)
                for word in sorted(self.keywords, key=len, reverse=True)
            ),
            re.IGNORECASE,
        )

    def match_keyword(self, text: str, position: int):
        found = self._pattern.match(text, position)
        if not found:
            return None
        word = found.group(0).upper()
        return word, self.keywords[word], found.end()


#: Keyword order. Operators and functions come first because almost every
#: line uses them, so they take one byte rather than two.
ORDERED_KEYWORDS = OPERATOR_KEYWORDS + FUNCTION_KEYWORDS + BASE_KEYWORDS

AMIGABASIC_10 = Dialect("AmigaBASIC 1.0", ORDERED_KEYWORDS)
AMIGABASIC_12 = Dialect("AmigaBASIC 1.2", ORDERED_KEYWORDS, EXTENDED_KEYWORDS)

DIALECTS = {
    AMIGABASIC_10.name: AMIGABASIC_10,
    AMIGABASIC_12.name: AMIGABASIC_12,
}


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------
def _scan_line_tokens(data: bytes, start: int, end: int, dialect: Dialect) -> list[Token]:
    tokens: list[Token] = []
    position = start
    while position < end:
        byte = data[position]
        if byte == LINE_NUMBER_TOKEN:
            value = decode_line_number(data[position + 1 : position + 4])
            tokens.append(
                Token(TokenKind.LINENUM, byte, value, position, position + 4, str(value))
            )
            position += 4
            continue
        if byte in dialect.escape:
            following = data[position + 1] if position + 1 < end else 0
            word = dialect.escape[byte].get(following)
            if word is None:
                raise DataError(
                    f"Unknown extended keyword &{following:02X} at offset {position}."
                )
            tokens.append(
                Token(TokenKind.KEYWORD, byte, word, position, position + 2, word)
            )
            position += 2
            continue
        if byte >= 0x80:
            word = dialect.tokens.get(byte)
            if word is None:
                raise DataError(f"Unknown keyword token &{byte:02X} at offset {position}.")
            kind = TokenKind.REM if word in {"REM", "'"} else TokenKind.KEYWORD
            tokens.append(Token(kind, byte, word, position, position + 1, word))
            position += 1
            if kind is TokenKind.REM:
                text = data[position:end].decode("latin-1")
                tokens.append(
                    Token(TokenKind.STRING, 0, text, position, end, text)
                )
                position = end
            continue
        if byte == 0x22:  # a quoted string
            close = data.find(b'"', position + 1, end)
            close = end - 1 if close < 0 else close
            text = data[position + 1 : close].decode("latin-1")
            tokens.append(
                Token(TokenKind.STRING, 0, text, position, close + 1, f'"{text}"')
            )
            position = close + 1
            continue
        run_start = position
        if chr(byte).isdigit() or byte == 0x2E:
            while position < end and (
                chr(data[position]).isdigit() or data[position] in b".eE+-"
            ):
                if data[position] in b"+-" and data[position - 1] not in b"eE":
                    break
                position += 1
            text = data[run_start:position].decode("latin-1")
            tokens.append(Token(TokenKind.NUMBER, 0, text, run_start, position, text))
            continue
        if chr(byte).isalpha() or byte == 0x5F:
            while position < end and (
                chr(data[position]).isalnum() or data[position] in b"_$%!#&."
            ):
                position += 1
            text = data[run_start:position].decode("latin-1")
            tokens.append(Token(TokenKind.IDENT, 0, text, run_start, position, text))
            continue
        text = chr(byte)
        tokens.append(Token(TokenKind.SYMBOL, 0, text, position, position + 1, text))
        position += 1
    return tokens


def scan_program(data: bytes, dialect: Dialect = AMIGABASIC_10):
    """Yield one ``Line`` per numbered line, without building the whole listing."""
    if not data:
        return
    position = 1 if data[:1] == bytes((BASIC_MAGIC,)) else 0
    while position < len(data):
        length = data[position]
        if length == 0:
            return
        if position + length > len(data):
            raise DataError(
                f"The line at offset {position} declares {length} bytes but only "
                f"{len(data) - position} remain."
            )
        if length < 4:
            raise DataError(f"The line at offset {position} is too short to be valid.")
        line_number = (data[position + 1] << 8) | data[position + 2]
        end = position + length - 1
        tokens = _scan_line_tokens(data, position + 3, end, dialect)
        yield Line(line_number=line_number, start=position, end=position + length, tokens=tokens)
        position += length


# ---------------------------------------------------------------------------
# Detokenising
# ---------------------------------------------------------------------------
def detokenise(data: bytes, dialect: Dialect = AMIGABASIC_10) -> str:
    """Render a tokenised program as its source listing."""
    lines = []
    for line in scan_program(data, dialect=dialect):
        body = "".join(token.text for token in line.tokens)
        lines.append(f"{line.line_number} {body}".rstrip())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tokenising
# ---------------------------------------------------------------------------
_LINE_START = re.compile(r"\s*(\d+)\s?(.*)$")
_JUMP_KEYWORDS = {"GOTO", "GOSUB", "THEN", "ELSE", "RESTORE", "RUN", "RESUME", "LIST"}


def _tokenise_body(body: str, dialect: Dialect) -> bytes:
    out = bytearray()
    position = 0
    last_keyword = ""
    while position < len(body):
        character = body[position]
        if character == '"':
            close = body.find('"', position + 1)
            close = len(body) if close < 0 else close
            out.extend(body[position : close + 1].encode("latin-1", "replace"))
            position = close + 1 if close < len(body) else len(body)
            continue
        matched = dialect.match_keyword(body, position)
        if matched and (position == 0 or not (body[position - 1].isalnum() or body[position - 1] == "_")):
            word, encoded, end = matched
            # A word only tokenises as a keyword when it is not glued to an
            # identifier on either side, so ``FORMAT`` stays a variable name.
            following = body[end] if end < len(body) else ""
            if not (following.isalnum() or following == "_"):
                out.extend(encoded)
                position = end
                last_keyword = word
                if word in {"REM", "'"}:
                    out.extend(body[position:].encode("latin-1", "replace"))
                    return bytes(out)
                continue
        if character.isdigit() and last_keyword in _JUMP_KEYWORDS:
            run = position
            while run < len(body) and body[run].isdigit():
                run += 1
            value = int(body[position:run])
            if value > MAX_LINE_NUMBER:
                raise DataError(f"{value} is not a valid line number.")
            out.append(LINE_NUMBER_TOKEN)
            out.extend(encode_line_number(value))
            position = run
            continue
        if character.isalnum() or character == "_":
            run = position
            while run < len(body) and (body[run].isalnum() or body[run] in "_$%!#&."):
                run += 1
            out.extend(body[position:run].encode("latin-1", "replace"))
            position = run
            continue
        if not character.isspace() or character == " ":
            out.extend(character.encode("latin-1", "replace"))
        if character not in " ":
            last_keyword = ""
        position += 1
    return bytes(out)


def tokenise(source: str, dialect: Dialect = AMIGABASIC_10) -> bytes:
    """Tokenise a numbered listing into an AmigaBASIC program."""
    program = bytearray((BASIC_MAGIC,))
    seen: set[int] = set()
    previous = -1
    found_any = False
    for raw in str(source).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw.strip():
            continue
        match = _LINE_START.match(raw)
        if not match:
            raise DataError(f"Every AmigaBASIC line needs a line number: {raw.strip()!r}")
        number = int(match.group(1))
        if number > MAX_LINE_NUMBER:
            raise DataError(f"Line number {number} is above {MAX_LINE_NUMBER}.")
        if number in seen:
            raise DataError(f"Line {number} appears more than once.")
        if number < previous:
            raise DataError(f"Line {number} is out of order.")
        seen.add(number)
        previous = number
        found_any = True
        body = _tokenise_body(match.group(2), dialect)
        length = len(body) + 4
        if length > MAX_LINE_BYTES:
            raise DataError(
                f"Line {number} tokenises to {length} bytes, above the {MAX_LINE_BYTES}-byte limit."
            )
        program.append(length)
        program.append((number >> 8) & 0xFF)
        program.append(number & 0xFF)
        program.extend(body)
        program.append(0)
    if not found_any:
        raise DataError("The listing contains no numbered lines.")
    program.append(0)
    return bytes(program)


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------
def detect(data: bytes) -> Detection:
    """Decide whether these bytes begin with a tokenised AmigaBASIC program."""
    if len(data) < 6 or data[0] != BASIC_MAGIC:
        return Detection(Verdict.NOT_BASIC, None, "No AmigaBASIC magic byte.")
    position = 1
    lines = 0
    previous = -1
    while position < len(data):
        length = data[position]
        if length == 0:
            position += 1
            break
        if length < 4 or position + length > len(data):
            return Detection(
                Verdict.DAMAGED, None, f"The line at offset {position} is malformed."
            )
        number = (data[position + 1] << 8) | data[position + 2]
        if number > MAX_LINE_NUMBER or number < previous:
            return Detection(
                Verdict.DAMAGED, None, f"Line {number} at offset {position} is out of order."
            )
        if data[position + length - 1] != 0:
            return Detection(
                Verdict.DAMAGED, None, f"The line at offset {position} is unterminated."
            )
        previous = number
        lines += 1
        position += length
    if not lines:
        return Detection(Verdict.NOT_BASIC, None, "No numbered lines were found.")
    if position < len(data):
        return Detection(
            Verdict.BASIC_TRAILING,
            position,
            f"{len(data) - position:,} trailing bytes follow the program.",
        )
    return Detection(Verdict.BASIC, position, f"{lines} line(s).")


def is_tokenised(data: bytes) -> bool:
    return detect(data).verdict in {Verdict.BASIC, Verdict.BASIC_TRAILING}


__all__ = [
    "AMIGABASIC_10",
    "AMIGABASIC_12",
    "BASIC_MAGIC",
    "DIALECTS",
    "Detection",
    "Dialect",
    "ESCAPE_BYTE",
    "EXTENDED_ESCAPE_BYTE",
    "Line",
    "MAX_LINE_BYTES",
    "Token",
    "TokenKind",
    "Verdict",
    "decode_line_number",
    "detect",
    "detokenise",
    "encode_line_number",
    "is_tokenised",
    "scan_program",
    "tokenise",
]
