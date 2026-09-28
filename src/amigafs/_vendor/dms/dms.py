"""DiskMasher (DMS) archives: a whole Amiga floppy, compressed track by track.

DMS was how Amiga disks travelled. Unlike an ADF, which is just the sectors,
a DMS carries the *tracks*: their numbers, their CRCs, and the compression
mode each one was packed with. That is why a DMS can hold a disk an ADF
cannot represent, and why converting one is a real operation rather than a
rename.

The structure is a 56-byte archive header followed by a run of 20-byte track
headers, each with its packed data inline. Every track is checked twice: a
CRC-16 of the packed data and a 16-bit sum of the unpacked data, as xDMS
does, so a truncated download is detected rather than silently producing a
disk full of zeros.

Every mode is unpacked in-tree: ``NOCOMP``, ``SIMPLE``, ``QUICK``, ``MEDIUM``,
``DEEP``, ``HEAVY1`` and ``HEAVY2``, plus the
run-length pass that every mode may apply on top. The former note that
LZ-with-Huffman stages are not decoded here; a track packed with one of them
is listed with its real size and marked incomplete rather than being guessed
at.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .checksum import sha256_bytes
from .dms_codec import DMSDecoder, unpack_rle
from .errors import DMSError

MAGIC = b"DMS!"
HEADER_SIZE = 56
TRACK_HEADER_SIZE = 20
TRACK_MAGIC = b"TR"

#: A DMS track is one whole cylinder: two heads of eleven 512-byte sectors.
#: Eighty of them make the 880 KiB an AmigaDOS double-density disk holds.
TRACK_SIZE = 2 * 11 * 512
BLOCK_SIZE = 512

#: Tracks this short are not disk data. DiskMasher stores a FILE_ID.DIZ as
#: track 80, a banner as track 0xFFFF and sometimes an advertising boot block
#: as a short track 0; xDMS writes only tracks longer than this to the disk.
PSEUDO_TRACK_LIMIT = 2048

COMPRESSION_MODES = {
    0: "NOCOMP",
    1: "SIMPLE",
    2: "QUICK",
    3: "MEDIUM",
    4: "DEEP",
    5: "HEAVY1",
    6: "HEAVY2",
}

#: Every mode DiskMasher writes, all of which are decoded back to track data.
SUPPORTED_MODES = frozenset(COMPRESSION_MODES)

DISK_TYPES = {
    0: "Unknown",
    1: "AmigaDOS OFS",
    2: "AmigaDOS FFS",
    3: "AmigaDOS International OFS",
    4: "AmigaDOS International FFS",
    5: "AmigaDOS Directory Cache OFS",
    6: "AmigaDOS Directory Cache FFS",
    7: "Professional File System",
    8: "MS-DOS",
    9: "Claude/Amiga custom",
}

#: Track flags recorded in each track header.
FLAG_RLE = 0x02


#: The identity a saved DMS project carries, so a reader can tell which
#: release wrote it.
DMS_PROJECT_SCHEMA = "amiga-file-forge/dms-project/v1"


@dataclass(frozen=True)
class DMSFile:
    """One track, presented as a browsable member of the archive."""

    name: str
    unpacked_crc: int
    packed_crc: int
    data: bytes
    blocks: int
    complete: bool
    inferred_name: bool = False
    original_name: str | None = None
    number: int = 0
    mode: str = "NOCOMP"
    packed_length: int = 0
    unpacked_length: int = 0
    crc_ok: bool = True
    start: int = 0
    data_start: int = 0
    end: int = 0


@dataclass(frozen=True)
class DMSContents:
    """A decoded archive: its version, its tracks and anything questionable."""

    version: str
    files: tuple[DMSFile, ...]
    warnings: tuple[str, ...]
    chunk_counts: dict[int, int]
    info: dict | None = None

    @property
    def tracks(self) -> tuple[DMSFile, ...]:
        return self.files


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------
def crc16(data: bytes) -> int:
    """The CCITT CRC-16 DMS stores for each track, low bit first."""
    crc = 0
    for value in data:
        crc ^= value
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def simple_sum(data: bytes) -> int:
    """The 16-bit sum of bytes DMS stores for each track after unpacking.

    xDMS checks unpacked track data with this sum (``Calc_CheckSum``) and
    only the packed data and the headers with ``crc16``.
    """
    return sum(data) & 0xFFFF


# ---------------------------------------------------------------------------
# Unpackers
# ---------------------------------------------------------------------------
def _read_header(data: bytes) -> dict:
    if len(data) < HEADER_SIZE:
        raise DMSError("The file is shorter than a DMS archive header.")
    if data[:4] != MAGIC:
        raise DMSError(
            "The file does not begin with the DMS! signature, so it is not a "
            "DiskMasher archive."
        )
    (
        header_sum,
        info_bits,
        date,
        low_track,
        high_track,
        packed_size,
        unpacked_size,
        os_version,
        os_revision,
        cpu,
        coprocessor,
        machine,
        extra_cpu,
        speed,
        elapsed,
        created_with,
        needs_version,
        disk_type,
        compression,
        info_header,
    ) = struct.unpack_from(">IIIHHIIHHHHHHHIHHHHH", data, 4)
    return {
        "headerSum": header_sum,
        "infoBits": info_bits,
        "date": date,
        "lowTrack": low_track,
        "highTrack": high_track,
        "packedSize": packed_size,
        "unpackedSize": unpacked_size,
        "creatorVersion": f"{created_with >> 8}.{created_with & 0xFF:02d}",
        "requiredVersion": f"{needs_version >> 8}.{needs_version & 0xFF:02d}",
        "diskType": DISK_TYPES.get(disk_type, f"type {disk_type}"),
        "diskTypeCode": disk_type,
        "compression": COMPRESSION_MODES.get(compression, f"mode {compression}"),
        "compressionCode": compression,
        "cpu": cpu,
        "machine": machine,
        "osVersion": f"{os_version}.{os_revision}",
        "infoHeader": info_header,
    }


def _iter_tracks(data: bytes, warnings: list[str]):
    offset = HEADER_SIZE
    while offset + TRACK_HEADER_SIZE <= len(data):
        header = data[offset : offset + TRACK_HEADER_SIZE]
        if header[:2] != TRACK_MAGIC:
            warnings.append(
                f"Expected a track header at offset {offset:,} but found "
                f"{header[:2]!r}; the rest of the archive was not read."
            )
            return
        (
            number,
            _unused,
            packed_length,
            rle_length,
            unpacked_length,
            flags,
            mode,
            unpacked_crc,
            packed_crc,
            header_crc,
        ) = struct.unpack_from(">HHHHHBBHHH", header, 2)
        computed_header_crc = crc16(header[:TRACK_HEADER_SIZE - 2])
        payload_start = offset + TRACK_HEADER_SIZE
        payload = data[payload_start : payload_start + packed_length]
        if len(payload) != packed_length:
            warnings.append(
                f"Track {number} declares {packed_length:,} packed bytes but only "
                f"{len(payload):,} are present."
            )
        yield {
            "number": number,
            "packed_length": packed_length,
            "rle_length": rle_length,
            "unpacked_length": unpacked_length,
            "flags": flags,
            "mode": mode,
            "unpacked_crc": unpacked_crc,
            "packed_crc": packed_crc,
            "header_crc": header_crc,
            "header_crc_ok": computed_header_crc == header_crc,
            "payload": payload,
            "start": offset,
            "data_start": payload_start,
            "end": payload_start + packed_length,
        }
        offset = payload_start + packed_length


def parse_dms(data: bytes) -> DMSContents:
    """Decode an archive into its tracks, verifying every checksum on the way."""
    info = _read_header(data)
    warnings: list[str] = []
    files: list[DMSFile] = []
    mode_counts: dict[int, int] = {}

    decoder = DMSDecoder()
    for track in _iter_tracks(data, warnings):
        mode = int(track["mode"])
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        number = int(track["number"])
        packed_ok = crc16(track["payload"]) == track["packed_crc"]
        if not track["header_crc_ok"]:
            warnings.append(f"Track {number} has a bad header checksum.")
        if not packed_ok:
            warnings.append(f"Track {number} has a bad packed-data checksum.")
        payload = b""
        complete = False
        crc_ok = False
        if mode in SUPPORTED_MODES and packed_ok:
            try:
                payload = decoder.unpack_track(
                    track["payload"],
                    mode,
                    int(track["rle_length"]),
                    int(track["unpacked_length"]),
                    int(track["flags"]),
                )
                crc_ok = simple_sum(payload) == track["unpacked_crc"]
                complete = crc_ok and len(payload) == int(track["unpacked_length"])
                if not crc_ok:
                    warnings.append(f"Track {number} unpacked with a bad checksum.")
            except DMSError as error:
                warnings.append(
                    f"Track {number} ({COMPRESSION_MODES.get(mode, mode)}): {error}"
                )
        elif mode not in SUPPORTED_MODES:
            warnings.append(
                f"Track {number} uses compression mode {mode}, which DiskMasher "
                "does not define."
            )

        # Tracks 80 and above hold the file-note and banner blocks DMS adds;
        # they are not part of the disk, so they are named for what they are.
        if number >= 0x8000 or number > 200:
            name = f"Info {number & 0x7FFF}"
        else:
            name = f"Track {number:03d}"
        files.append(
            DMSFile(
                name=name,
                unpacked_crc=int(track["unpacked_crc"]),
                packed_crc=int(track["packed_crc"]),
                data=payload,
                blocks=max(0, len(payload) // BLOCK_SIZE),
                complete=complete,
                number=number,
                mode=COMPRESSION_MODES.get(mode, f"mode {mode}"),
                packed_length=int(track["packed_length"]),
                unpacked_length=int(track["unpacked_length"]),
                crc_ok=crc_ok,
                start=int(track["start"]),
                data_start=int(track["data_start"]),
                end=int(track["end"]),
            )
        )

    if not files:
        raise DMSError("The archive header is valid but it contains no tracks.")
    return DMSContents(
        version=info["creatorVersion"],
        files=tuple(files),
        warnings=tuple(dict.fromkeys(warnings)),
        chunk_counts=mode_counts,
        info=info,
    )


def is_disk_track(track: DMSFile) -> bool:
    """Whether a track holds disk data rather than a banner or FILE_ID.DIZ."""
    return track.number < 200 and track.unpacked_length > PSEUDO_TRACK_LIMIT


def to_adf(data: bytes) -> bytes:
    """Rebuild the complete disk image an archive was made from.

    Tracks are written at their declared positions rather than in the order
    they appear, so an archive that omits empty tracks -- which DiskMasher does
    by default -- still produces a correctly sized image with the gaps zeroed.
    The banner, FILE_ID.DIZ and advertising boot block pseudo-tracks are left
    out, and the cylinder size comes from the tracks themselves, so a
    high-density archive rebuilds at 22 sectors a track.
    """
    contents = parse_dms(data)
    disk_tracks = [track for track in contents.files if is_disk_track(track)]
    if not disk_tracks:
        raise DMSError("The archive contains no disk tracks to rebuild.")
    missing = [track.number for track in disk_tracks if not track.complete]
    if missing:
        raise DMSError(
            f"{len(missing)} track(s) could not be unpacked: "
            + ", ".join(str(number) for number in missing[:8])
            + ("…" if len(missing) > 8 else "")
        )
    cylinder_size = disk_tracks[0].unpacked_length
    if any(track.unpacked_length != cylinder_size for track in disk_tracks):
        raise DMSError("The archive mixes tracks of different sizes, so it is not one disk.")
    highest = max(track.number for track in disk_tracks)
    image = bytearray(cylinder_size * (highest + 1))
    for track in disk_tracks:
        start = track.number * cylinder_size
        image[start : start + len(track.data)] = track.data
    return bytes(image)


# ---------------------------------------------------------------------------
# Project view and guarded replacement
# ---------------------------------------------------------------------------
def dms_project(data: bytes) -> dict:
    """Describe an archive for the workbench's project pane."""
    contents = parse_dms(data)
    info = contents.info or {}
    return {
        "schema": DMS_PROJECT_SCHEMA,
        "version": contents.version,
        "requiredVersion": info.get("requiredVersion"),
        "diskType": info.get("diskType"),
        "compression": info.get("compression"),
        "lowTrack": info.get("lowTrack"),
        "highTrack": info.get("highTrack"),
        "packedSize": info.get("packedSize"),
        "unpackedSize": info.get("unpackedSize"),
        "checksum": sha256_bytes(data),
        "warnings": list(contents.warnings),
        "modes": {
            COMPRESSION_MODES.get(mode, str(mode)): count
            for mode, count in sorted(contents.chunk_counts.items())
        },
        "tracks": [
            {
                "index": index,
                "name": track.name,
                "number": track.number,
                "mode": track.mode,
                "packedLength": track.packed_length,
                "unpackedLength": track.unpacked_length,
                "length": len(track.data),
                "complete": track.complete,
                "checksumValid": track.crc_ok,
                "offset": track.start,
            }
            for index, track in enumerate(contents.files)
        ],
    }


def dms_editability(data: bytes, file_index: int) -> dict:
    """Report whether one track can be replaced without rebuilding the archive."""
    contents = parse_dms(data)
    if not 0 <= file_index < len(contents.files):
        raise DMSError("That track is not in this archive.")
    track = contents.files[file_index]
    reasons: list[str] = []
    if track.mode != "NOCOMP":
        reasons.append(
            f"The track is stored with {track.mode} compression, which this build "
            "cannot re-pack."
        )
    if not track.complete:
        reasons.append("The track did not unpack cleanly, so its contents are unknown.")
    return {
        "index": file_index,
        "name": track.name,
        "editable": not reasons,
        "sameLengthOnly": True,
        "length": len(track.data),
        "mode": track.mode,
        "reasons": reasons,
    }


def replace_dms_file(data: bytes, file_index: int, replacement: bytes) -> tuple[bytes, dict]:
    """Replace one uncompressed track in place, keeping every offset intact.

    Only a same-length replacement of a ``NOCOMP`` track is allowed. Re-packing
    a track would move every following track and invalidate the archive's own
    size fields, so the workbench refuses rather than producing an archive that
    only it can read.
    """
    contents = parse_dms(data)
    if not 0 <= file_index < len(contents.files):
        raise DMSError("That track is not in this archive.")
    track = contents.files[file_index]
    editability = dms_editability(data, file_index)
    if not editability["editable"]:
        raise DMSError(editability["reasons"][0])
    if len(replacement) != len(track.data):
        raise DMSError(
            f"{track.name} holds {len(track.data):,} bytes; the replacement has "
            f"{len(replacement):,}. A DMS track can only be replaced by one of "
            "exactly the same length."
        )
    rebuilt = bytearray(data)
    rebuilt[track.data_start : track.end] = replacement
    header_start = track.start
    # Offset 14 is the sum of the unpacked data, offset 16 the CRC of the
    # stored data; for an uncompressed track the two cover the same bytes.
    struct.pack_into(">H", rebuilt, header_start + 14, simple_sum(replacement))
    struct.pack_into(">H", rebuilt, header_start + 16, crc16(replacement))
    struct.pack_into(
        ">H",
        rebuilt,
        header_start + 18,
        crc16(bytes(rebuilt[header_start : header_start + TRACK_HEADER_SIZE - 2])),
    )
    report = {
        "index": file_index,
        "name": track.name,
        "length": len(replacement),
        "checksum": sha256_bytes(bytes(rebuilt)),
    }
    return bytes(rebuilt), report


# ---------------------------------------------------------------------------
# Content helpers shared with the workbench
# ---------------------------------------------------------------------------
def is_tokenized_basic(data: bytes) -> bool:
    """True when these bytes are a tokenised AmigaBASIC program."""
    try:
        from amiganut.basic import is_tokenised
    except ImportError:  # pragma: no cover - defensive
        return False
    return is_tokenised(data)


def basic_unopened_channel_io(data: bytes) -> bool:
    """True when a BASIC program reads or writes a file it never opened.

    A converted loader that assumed a disk was already inserted is the most
    common reason a rebuilt image starts and then stops with an error, so this
    is checked before a conversion is offered rather than after it fails.
    """
    try:
        from amiganut.basic import AMIGABASIC_12, detokenise
    except ImportError:  # pragma: no cover - defensive
        return False
    if not is_tokenized_basic(data):
        return False
    try:
        listing = detokenise(data, dialect=AMIGABASIC_12).upper()
    except Exception:
        return False
    opens = listing.count("OPEN ")
    reads = sum(listing.count(word) for word in ("INPUT#", "PRINT#", "GET #", "PUT #"))
    return reads > 0 and opens == 0


def rewrite_basic_loader(
    data: bytes,
    launch_name: str,
    name_map: dict[str, str] | None = None,
) -> tuple[bytes, list[str]]:
    """Point a loader at its files' new names and device.

    A loader lifted out of a DMS still refers to ``DF0:`` and to the names the
    files had on the floppy. Both change when the disk is installed to a hard
    drive, so both are rewritten here, and every substitution is reported so
    the user can see what was altered rather than having to diff the listing.
    """
    try:
        from amiganut.basic import AMIGABASIC_12, detokenise, tokenise
    except ImportError:  # pragma: no cover - defensive
        return data, []
    if not is_tokenized_basic(data):
        return data, []
    try:
        listing = detokenise(data, dialect=AMIGABASIC_12)
    except Exception:
        return data, []

    changes: list[str] = []
    updated = listing
    for device in ("DF0:", "DF1:", "df0:", "df1:"):
        if device in updated:
            updated = updated.replace(device, "")
            changes.append(f"Removed the {device.upper()} device prefix so the loader uses its own directory.")
    for original, replacement in (name_map or {}).items():
        if original and replacement and original != replacement and original in updated:
            updated = updated.replace(original, replacement)
            changes.append(f"Renamed the reference to {original} as {replacement}.")
    if launch_name and launch_name not in updated:
        changes.append(f"The loader does not mention {launch_name}; check its first line.")
    if updated == listing:
        return data, changes
    try:
        return tokenise(updated), changes
    except Exception as error:
        return data, [*changes, f"The rewritten loader could not be tokenised: {error}"]


__all__ = [
    "BLOCK_SIZE",
    "COMPRESSION_MODES",
    "DISK_TYPES",
    "DMSContents",
    "DMSError",
    "DMSFile",
    "HEADER_SIZE",
    "PSEUDO_TRACK_LIMIT",
    "SUPPORTED_MODES",
    "TRACK_HEADER_SIZE",
    "TRACK_SIZE",
    "basic_unopened_channel_io",
    "crc16",
    "dms_editability",
    "dms_project",
    "is_disk_track",
    "is_tokenized_basic",
    "parse_dms",
    "replace_dms_file",
    "rewrite_basic_loader",
    "simple_sum",
    "to_adf",
    "unpack_rle",
]
