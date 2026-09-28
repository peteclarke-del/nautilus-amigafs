"""The DiskMasher decompressors, in one place and in pure Python.

DMS packs a disk one track at a time, and each track records which of seven
modes it was packed with. The modes are not independent of one another: they
share a single dictionary buffer that carries over from track to track, so a
track cannot be unpacked in isolation, and the tracks have to be decoded in the
order the archive stores them.

The algorithms are ports of xDMS 1.3 by Andre Rodrigues de la Rocha, which its
author released into the public domain ("You can spread it, modify it and use
it in any way you like"). They are reproduced here rather than shelled out to
because the workbench must open a DMS on every platform it runs on, without
asking the user to install a second tool first.

Correctness is not assumed. Every track's unpacked bytes are checked against
the checksum the archive stores for them, so a decode that goes wrong is
reported rather than written to a disk image.
"""

from __future__ import annotations

from .errors import DMSError


class DMSCodecError(DMSError):
    """A track could not be unpacked."""


#: The shared dictionary DMS decompresses into. Every mode indexes it with its
#: own mask, and its contents deliberately survive from one track to the next.
TEXT_SIZE = 0x4000

#: Position codes and their extra-bit counts, used by MEDIUM and DEEP to turn
#: one byte into a dictionary offset. Both tables are xDMS's, written here as
#: the runs they are made of so the shape of the encoding stays visible: the
#: nearer a match is, the fewer bits its offset needs.
def _expand(runs: tuple[tuple[int, int], ...]) -> bytes:
    values: list[int] = []
    for value, count in runs:
        values.extend([value] * count)
    return bytes(values)


D_CODE = _expand(
    (
        (0x00, 32), (0x01, 16), (0x02, 16), (0x03, 16),
        *((code, 8) for code in range(0x04, 0x0C)),
        *((code, 4) for code in range(0x0C, 0x18)),
        *((code, 2) for code in range(0x18, 0x30)),
        *((code, 1) for code in range(0x30, 0x40)),
    )
)

D_LEN = _expand(((3, 32), (4, 48), (5, 64), (6, 48), (7, 48), (8, 16)))

assert len(D_CODE) == 256 and len(D_LEN) == 256


class _BitReader:
    """xDMS's ``GETBITS``/``DROPBITS`` pair, with its exact refill behaviour.

    The reader keeps at least sixteen bits buffered and always refills a whole
    byte at a time, which is what makes the bit positions of the Huffman modes
    line up. Reading past the end of a track yields zero bits rather than
    failing: a packed track legitimately ends mid-byte, and the decoders stop
    on the output length rather than on the input.
    """

    __slots__ = ("data", "position", "bitbuf", "bitcount")

    def __init__(self, data: bytes):
        self.data = data
        self.position = 0
        self.bitbuf = 0
        self.bitcount = 0
        self.drop(0)

    def _next_byte(self) -> int:
        if self.position >= len(self.data):
            self.position += 1
            return 0
        value = self.data[self.position]
        self.position += 1
        return value

    def get(self, count: int) -> int:
        if count == 0:
            return 0
        return (self.bitbuf >> (self.bitcount - count)) & ((1 << count) - 1)

    def drop(self, count: int) -> None:
        self.bitcount -= count
        self.bitbuf &= (1 << self.bitcount) - 1 if self.bitcount > 0 else 0
        while self.bitcount < 16:
            self.bitbuf = (self.bitbuf << 8) | self._next_byte()
            self.bitcount += 8

    def take(self, count: int) -> int:
        value = self.get(count)
        self.drop(count)
        return value


def unpack_rle(data: bytes, expected: int) -> bytes:
    """Undo the run-length pass DMS applies on top of most modes.

    ``0x90`` is the escape. ``90 00`` is a literal ``0x90``. Otherwise the two
    bytes after the escape are the repeat count and the byte to repeat, and a
    count of ``0xFF`` means a sixteen-bit count follows instead. The value
    comes from the stream, not from whatever preceded the escape.
    """
    out = bytearray()
    index = 0
    length = len(data)
    while len(out) < expected:
        if index >= length:
            raise DMSCodecError("The packed track ended in the middle of a run.")
        value = data[index]
        index += 1
        if value != 0x90:
            out.append(value)
            continue
        if index >= length:
            raise DMSCodecError("A run-length escape runs past the end of the track.")
        count = data[index]
        index += 1
        if count == 0:
            out.append(0x90)
            continue
        if index >= length:
            raise DMSCodecError("A run-length run names no byte to repeat.")
        repeated = data[index]
        index += 1
        if count == 0xFF:
            if index + 2 > length:
                raise DMSCodecError("A long run-length count is truncated.")
            count = (data[index] << 8) | data[index + 1]
            index += 2
        if len(out) + count > expected:
            raise DMSCodecError("A run-length run overflows the track.")
        out.extend(bytes((repeated,)) * count)
    return bytes(out)


def _make_table(count: int, lengths: list[int], table_bits: int, table: list[int],
                left: list[int], right: list[int]) -> None:
    """Build one canonical Huffman lookup table, as LHA and xDMS build it."""
    state = {
        "n": count,
        "avail": count,
        "table_size": 1 << table_bits,
        "bit": (1 << table_bits) // 2,
        "max_depth": table_bits + 1,
        "depth": 1,
        "len": 1,
        "c": -1,
        "codeword": 0,
    }

    def walk() -> int:
        index = 0
        if state["len"] == state["depth"]:
            while True:
                state["c"] += 1
                if state["c"] >= state["n"]:
                    break
                if lengths[state["c"]] == state["len"]:
                    index = state["codeword"]
                    state["codeword"] += state["bit"]
                    if state["codeword"] > state["table_size"]:
                        raise DMSCodecError("The Huffman table overflows its size.")
                    while index < state["codeword"]:
                        table[index] = state["c"]
                        index += 1
                    return state["c"]
            state["c"] = -1
            state["len"] += 1
            state["bit"] >>= 1
        state["depth"] += 1
        if state["depth"] < state["max_depth"]:
            walk()
            walk()
        elif state["depth"] > 32:
            raise DMSCodecError("The Huffman table is deeper than the format allows.")
        else:
            index = state["avail"]
            state["avail"] += 1
            if index >= 2 * state["n"] - 1:
                raise DMSCodecError("The Huffman table ran out of nodes.")
            left[index] = walk()
            right[index] = walk()
            if state["codeword"] >= state["table_size"]:
                raise DMSCodecError("The Huffman table overflows while descending.")
            if state["depth"] == state["max_depth"]:
                table[state["codeword"]] = index
                state["codeword"] += 1
        state["depth"] -= 1
        return index

    walk()
    walk()
    if state["codeword"] != state["table_size"]:
        raise DMSCodecError("The Huffman table is incomplete.")


# ---------------------------------------------------------------------------
# DEEP: LZ with the adaptive Huffman tree from LZHUF
# ---------------------------------------------------------------------------
_DEEP_F = 60
_DEEP_THRESHOLD = 2
_DEEP_N_CHAR = 256 - _DEEP_THRESHOLD + _DEEP_F
_DEEP_T = _DEEP_N_CHAR * 2 - 1
_DEEP_R = _DEEP_T - 1
_DEEP_MAX_FREQ = 0x8000

# ---------------------------------------------------------------------------
# HEAVY: LZ with the static Huffman trees from LHA
# ---------------------------------------------------------------------------
_HEAVY_NC = 510
_HEAVY_NPT = 20
_HEAVY_N1 = 510
_HEAVY_OFFSET = 253


class DMSDecoder:
    """Decode a DMS archive's tracks in order, sharing one dictionary.

    An instance is the archive's decompression state. Tracks must be given to
    it in the order the archive stores them, because every mode reads back
    through the dictionary the previous tracks filled.
    """

    def __init__(self) -> None:
        self.text = bytearray(TEXT_SIZE)
        self.left = [0] * (2 * _HEAVY_NC - 1)
        self.right = [0] * (2 * _HEAVY_NC - 1 + 9)
        self.c_len = [0] * _HEAVY_NC
        self.pt_len = [0] * _HEAVY_NPT
        self.c_table = [0] * 4096
        self.pt_table = [0] * 256
        self.lastlen = 0
        self.np = 0
        self.freq = [0] * (_DEEP_T + 1)
        self.prnt = [0] * (_DEEP_T + _DEEP_N_CHAR)
        self.son = [0] * _DEEP_T
        self.reset()

    def reset(self) -> None:
        """Return every decompressor to the state a new archive starts in."""
        self.quick_text_loc = 251
        self.medium_text_loc = 0x3FBE
        self.heavy_text_loc = 0
        self.deep_text_loc = 0x3FC4
        self.init_deep_tabs = True
        self.text = bytearray(TEXT_SIZE)

    # ---- track dispatch -------------------------------------------------
    def unpack_track(
        self,
        payload: bytes,
        mode: int,
        packed_length: int,
        unpacked_length: int,
        flags: int,
    ) -> bytes:
        """Unpack one track, then reset the decoders if the track says to.

        ``packed_length`` is the length after the mode's own decompression and
        before the run-length pass; ``unpacked_length`` is the finished track.
        """
        if mode == 0:
            data = payload[:unpacked_length]
        elif mode == 1:
            data = unpack_rle(payload, unpacked_length)
        elif mode == 2:
            data = unpack_rle(self._quick(payload, packed_length), unpacked_length)
        elif mode == 3:
            data = unpack_rle(self._medium(payload, packed_length), unpacked_length)
        elif mode == 4:
            data = unpack_rle(self._deep(payload, packed_length), unpacked_length)
        elif mode in {5, 6}:
            heavy_flags = (flags & 7) if mode == 5 else (flags | 8)
            data = self._heavy(payload, heavy_flags, packed_length)
            if flags & 4:
                data = unpack_rle(data, unpacked_length)
        else:
            raise DMSCodecError(f"Track compression mode {mode} is not a DMS mode.")
        if not flags & 1:
            self.reset()
        if len(data) < unpacked_length:
            raise DMSCodecError(
                f"The track unpacked to {len(data):,} bytes rather than "
                f"{unpacked_length:,}."
            )
        return bytes(data[:unpacked_length])

    # ---- QUICK ----------------------------------------------------------
    def _quick(self, data: bytes, expected: int) -> bytes:
        reader = _BitReader(data)
        out = bytearray()
        text = self.text
        loc = self.quick_text_loc
        while len(out) < expected:
            if reader.get(1):
                reader.drop(1)
                value = reader.take(8)
                out.append(value)
                text[loc & 0xFF] = value
                loc += 1
            else:
                reader.drop(1)
                length = reader.take(2) + 2
                offset = (loc - reader.take(8) - 1) & 0xFFFF
                for _ in range(length):
                    value = text[offset & 0xFF]
                    out.append(value)
                    text[loc & 0xFF] = value
                    loc += 1
                    offset += 1
        self.quick_text_loc = (loc + 5) & 0xFF
        return bytes(out[:expected])

    # ---- MEDIUM ---------------------------------------------------------
    def _medium(self, data: bytes, expected: int) -> bytes:
        mask = 0x3FFF
        reader = _BitReader(data)
        out = bytearray()
        text = self.text
        loc = self.medium_text_loc
        while len(out) < expected:
            if reader.get(1):
                reader.drop(1)
                value = reader.take(8)
                out.append(value)
                text[loc & mask] = value
                loc += 1
                continue
            reader.drop(1)
            code = reader.take(8)
            length = D_CODE[code] + 3
            bits = D_LEN[code]
            code = ((code << bits) | reader.take(bits)) & 0xFF
            bits = D_LEN[code]
            code = (D_CODE[code] << 8) | (((code << bits) | reader.take(bits)) & 0xFF)
            source = (loc - code - 1) & 0xFFFF
            for _ in range(length):
                value = text[source & mask]
                out.append(value)
                text[loc & mask] = value
                loc += 1
                source += 1
        self.medium_text_loc = (loc + 66) & mask
        return bytes(out[:expected])

    # ---- DEEP -----------------------------------------------------------
    def _init_deep_tables(self) -> None:
        for index in range(_DEEP_N_CHAR):
            self.freq[index] = 1
            self.son[index] = index + _DEEP_T
            self.prnt[index + _DEEP_T] = index
        index = 0
        node = _DEEP_N_CHAR
        while node <= _DEEP_R:
            self.freq[node] = self.freq[index] + self.freq[index + 1]
            self.son[node] = index
            self.prnt[index] = self.prnt[index + 1] = node
            index += 2
            node += 1
        self.freq[_DEEP_T] = 0xFFFF
        self.prnt[_DEEP_R] = 0
        self.init_deep_tabs = False

    def _deep_reconstruct(self) -> None:
        target = 0
        for index in range(_DEEP_T):
            if self.son[index] >= _DEEP_T:
                self.freq[target] = (self.freq[index] + 1) // 2
                self.son[target] = self.son[index]
                target += 1
        index = 0
        node = _DEEP_N_CHAR
        while node < _DEEP_T:
            total = self.freq[index] + self.freq[index + 1]
            self.freq[node] = total
            position = node - 1
            while total < self.freq[position]:
                position -= 1
            position += 1
            self.freq[position + 1 : node + 1] = self.freq[position:node]
            self.freq[position] = total
            self.son[position + 1 : node + 1] = self.son[position:node]
            self.son[position] = index
            index += 2
            node += 1
        for index in range(_DEEP_T):
            child = self.son[index]
            if child >= _DEEP_T:
                self.prnt[child] = index
            else:
                self.prnt[child] = self.prnt[child + 1] = index

    def _deep_update(self, code: int) -> None:
        if self.freq[_DEEP_R] == _DEEP_MAX_FREQ:
            self._deep_reconstruct()
        node = self.prnt[code + _DEEP_T]
        while True:
            self.freq[node] += 1
            frequency = self.freq[node]
            higher = node + 1
            if frequency > self.freq[higher]:
                while frequency > self.freq[higher + 1]:
                    higher += 1
                self.freq[node] = self.freq[higher]
                self.freq[higher] = frequency
                first = self.son[node]
                self.prnt[first] = higher
                if first < _DEEP_T:
                    self.prnt[first + 1] = higher
                second = self.son[higher]
                self.son[higher] = first
                self.prnt[second] = node
                if second < _DEEP_T:
                    self.prnt[second + 1] = node
                self.son[node] = second
                node = higher
            node = self.prnt[node]
            if node == 0:
                break

    def _deep_char(self, reader: _BitReader) -> int:
        node = self.son[_DEEP_R]
        while node < _DEEP_T:
            node = self.son[node + reader.take(1)]
        node -= _DEEP_T
        self._deep_update(node)
        return node

    def _deep_position(self, reader: _BitReader) -> int:
        byte = reader.take(8)
        high = D_CODE[byte] << 8
        bits = D_LEN[byte]
        low = ((byte << bits) | reader.take(bits)) & 0xFF
        return high | low

    def _deep(self, data: bytes, expected: int) -> bytes:
        mask = 0x3FFF
        reader = _BitReader(data)
        if self.init_deep_tabs:
            self._init_deep_tables()
        out = bytearray()
        text = self.text
        loc = self.deep_text_loc
        while len(out) < expected:
            code = self._deep_char(reader)
            if code < 256:
                out.append(code)
                text[loc & mask] = code
                loc += 1
                continue
            length = code - 255 + _DEEP_THRESHOLD
            source = (loc - self._deep_position(reader) - 1) & 0xFFFF
            for _ in range(length):
                value = text[source & mask]
                out.append(value)
                text[loc & mask] = value
                loc += 1
                source += 1
        self.deep_text_loc = (loc + 60) & mask
        return bytes(out[:expected])

    # ---- HEAVY ----------------------------------------------------------
    def _heavy_read_tree_c(self, reader: _BitReader) -> None:
        count = reader.take(9)
        if count > _HEAVY_NC:
            # DiskMasher never writes more code lengths than the tree holds.
            # A stream that says it does is damaged, and is refused rather
            # than read past the end of the table.
            raise DMSCodecError(
                f"A Heavy track declares {count} code lengths, more than the "
                f"{_HEAVY_NC} the format defines."
            )
        if count > 0:
            for index in range(count):
                self.c_len[index] = reader.take(5)
            for index in range(count, _HEAVY_NC):
                self.c_len[index] = 0
            _make_table(_HEAVY_NC, self.c_len, 12, self.c_table, self.left, self.right)
        else:
            value = reader.take(9)
            self.c_len = [0] * _HEAVY_NC
            self.c_table = [value] * 4096

    def _heavy_read_tree_p(self, reader: _BitReader) -> None:
        count = reader.take(5)
        if count > _HEAVY_NPT:
            raise DMSCodecError(
                f"A Heavy track declares {count} position lengths, more than the "
                f"{_HEAVY_NPT} the format defines."
            )
        if count > 0:
            for index in range(count):
                self.pt_len[index] = reader.take(4)
            for index in range(count, self.np):
                self.pt_len[index] = 0
            _make_table(self.np, self.pt_len, 8, self.pt_table, self.left, self.right)
        else:
            value = reader.take(5)
            self.pt_len = [0] * _HEAVY_NPT
            self.pt_table = [value] * 256

    def _heavy_decode_c(self, reader: _BitReader) -> int:
        node = self.c_table[reader.get(12)]
        if node < _HEAVY_N1:
            reader.drop(self.c_len[node])
            return node
        reader.drop(12)
        bits = reader.get(16)
        mask = 0x8000
        while node >= _HEAVY_N1:
            node = self.right[node] if bits & mask else self.left[node]
            mask >>= 1
        reader.drop(self.c_len[node] - 12)
        return node

    def _heavy_decode_p(self, reader: _BitReader) -> int:
        node = self.pt_table[reader.get(8)]
        if node < self.np:
            reader.drop(self.pt_len[node])
        else:
            reader.drop(8)
            bits = reader.get(16)
            mask = 0x8000
            while node >= self.np:
                node = self.right[node] if bits & mask else self.left[node]
                mask >>= 1
            reader.drop(self.pt_len[node] - 8)
        if node != self.np - 1:
            if node > 0:
                extra = node - 1
                node = reader.get(extra) | (1 << extra)
                reader.drop(extra)
            self.lastlen = node
        return self.lastlen

    def _heavy(self, data: bytes, flags: int, expected: int) -> bytes:
        # Heavy 1 works in a 4 KiB dictionary; Heavy 2 in an 8 KiB one.
        if flags & 8:
            self.np = 15
            mask = 0x1FFF
        else:
            self.np = 14
            mask = 0x0FFF
        reader = _BitReader(data)
        if flags & 2:
            self._heavy_read_tree_c(reader)
            self._heavy_read_tree_p(reader)
        out = bytearray()
        text = self.text
        loc = self.heavy_text_loc
        while len(out) < expected:
            code = self._heavy_decode_c(reader)
            if code < 256:
                out.append(code)
                text[loc & mask] = code
                loc += 1
                continue
            length = code - _HEAVY_OFFSET
            source = (loc - self._heavy_decode_p(reader) - 1) & 0xFFFF
            for _ in range(length):
                value = text[source & mask]
                out.append(value)
                text[loc & mask] = value
                loc += 1
                source += 1
        self.heavy_text_loc = loc & 0xFFFF
        return bytes(out[:expected])


__all__ = ["DMSCodecError", "DMSDecoder", "unpack_rle"]
