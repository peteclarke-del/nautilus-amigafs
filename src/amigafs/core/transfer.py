"""Explicit host transfers that preserve portable Amiga metadata.

The sidecar is the ``.inf`` record Amiga File Forge writes beside an exported
file, so a file exported by either tool imports into the other unchanged::

    Games/Program ----r-e- 00000007 "The game loader"

The fields are the path inside the volume, the protection bits as ``List``
prints them, the length in hexadecimal and the comment when there is one. The
datestamp travels as the host file's own modification time, as it does in Amiga
File Forge.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from amigafs._vendor.amiganut.file import (
    AMIGA_EPOCH,
    AmigaMeta,
    format_access_text,
)
from amigafs._vendor.amiganut.filesystem.blocks import MAX_COMMENT
from amigafs.core.image import AmigaImage, ImageNode, amiga_now, datestamp_from_ns
from amigafs.errors import AmigaFSError
from amigafs.i18n import _

TRANSFER_CHUNK_BYTES = 1024 * 1024
MAX_INF_BYTES = 4096
_INF_FIELDS = re.compile(r'"[^"]*"|\S+')
_PROTECTION_LETTERS = "hsparwed"
_INVERTED_BITS = 0x0F


@dataclass(frozen=True, slots=True)
class InfRecord:
    name: str | None
    metadata: AmigaMeta
    length: int | None


@dataclass(frozen=True, slots=True)
class ExportedFile:
    data_path: Path
    sidecar_path: Path
    amiga_path: str


@dataclass(frozen=True, slots=True)
class ImportedFile:
    node: ImageNode
    source_path: Path
    metadata_source: str


def parse_protection(text: str) -> int | None:
    """Read an eight-letter protection field such as ``----rwed``."""

    cleaned = text.strip()
    if len(cleaned) != len(_PROTECTION_LETTERS):
        return None
    value = 0
    for index, letter in enumerate(_PROTECTION_LETTERS):
        bit = 1 << (len(_PROTECTION_LETTERS) - 1 - index)
        character = cleaned[index]
        if character.casefold() == letter:
            present = True
        elif character == "-":
            present = False
        else:
            return None
        # A low bit is set when the operation is denied, so a printed letter
        # means the bit is clear.
        if present == bool(bit & _INVERTED_BITS):
            continue
        value |= bit
    return value


def _hex_field(value: str) -> int:
    return int(re.sub(r"^(?:&|0x)", "", value, flags=re.IGNORECASE), 16)


def parse_inf_record(data: bytes | str) -> InfRecord:
    """Parse an Amiga File Forge ``.inf`` record."""

    text = data.decode("latin-1", "replace") if isinstance(data, bytes) else data
    line = next((candidate.strip() for candidate in text.splitlines() if candidate.strip()), "")
    fields = _INF_FIELDS.findall(line)
    if len(fields) < 2:
        raise AmigaFSError(_("An INF sidecar must contain a path and its protection bits."))
    name = fields[0].strip('"')
    protection = parse_protection(fields[1])
    if protection is None:
        raise AmigaFSError(
            _(
                "The INF sidecar does not record Amiga protection bits. It may describe a "
                "file from another system."
            )
        )
    length: int | None = None
    remainder = fields[2:]
    if remainder and not remainder[0].startswith('"'):
        try:
            length = _hex_field(remainder[0])
        except ValueError:
            length = None
        else:
            remainder = remainder[1:]
    if length is not None and not 0 <= length <= 0xFFFFFFFF:
        raise AmigaFSError(_("The INF length does not fit in 32 bits."))
    comment = " ".join(remainder).strip('"') if remainder else ""
    if len(comment) > MAX_COMMENT:
        raise AmigaFSError(
            _("The INF comment is longer than {maximum} characters.").format(maximum=MAX_COMMENT)
        )
    return InfRecord(
        name=name or None,
        metadata=AmigaMeta(protection=protection, comment=comment),
        length=length,
    )


def format_inf_record(path: str, size: int, metadata: AmigaMeta) -> str:
    """Create one deterministic sidecar record from catalogue metadata."""

    catalogue_path = path.strip() or "File"
    if '"' in catalogue_path or '"' in metadata.comment:
        raise AmigaFSError(
            _("A path or comment containing a quote cannot be stored in an INF sidecar.")
        )
    if any(character.isspace() for character in catalogue_path):
        catalogue_path = f'"{catalogue_path}"'
    comment = " ".join(metadata.comment.split())
    trailing = f' "{comment}"' if comment else ""
    protection = format_access_text(int(metadata.protection) & 0xFF)
    return f"{catalogue_path} {protection} {size & 0xFFFFFFFF:08X}{trailing}\n"


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sidecar_candidates(source: Path) -> list[Path]:
    wanted = f"{source.name}.inf".casefold()
    return [child for child in source.parent.iterdir() if child.name.casefold() == wanted]


def _read_stable_file(source: Path, maximum_bytes: int) -> tuple[bytes, int]:
    """Read one unchanged host file without exceeding the image's free space."""

    with source.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if before.st_size > maximum_bytes:
            raise AmigaFSError(
                _("Host file needs {size} bytes but the volume has {free} bytes free.").format(
                    size=before.st_size, free=maximum_bytes
                )
            )
        data = handle.read(maximum_bytes + 1)
        after = os.fstat(handle.fileno())
    if len(data) > maximum_bytes:
        raise AmigaFSError(
            _("The host file grew beyond the volume's available space while reading.")
        )
    before_signature = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_signature = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_signature != after_signature or len(data) != before.st_size:
        raise AmigaFSError(
            _("The host file changed while it was being read; import was cancelled.")
        )
    return data, before.st_mtime_ns


def _import_metadata(
    source: Path, *, sidecar: str | Path | None, ignore_sidecar: bool
) -> tuple[str, AmigaMeta, int | None, str]:
    if sidecar is not None and ignore_sidecar:
        raise AmigaFSError(_("Specify either --sidecar or --ignore-sidecar, not both."))
    sidecar_path: Path | None = None
    if sidecar is not None:
        sidecar_path = Path(sidecar).expanduser().resolve()
        if not sidecar_path.is_file():
            raise AmigaFSError(
                _("INF sidecar does not exist or is not a file: {path}").format(path=sidecar_path)
            )
    elif not ignore_sidecar:
        candidates = _sidecar_candidates(source)
        if len(candidates) > 1:
            raise AmigaFSError(
                _("More than one case-insensitive INF sidecar matches {name}.").format(
                    name=source.name
                )
            )
        sidecar_path = candidates[0] if candidates else None

    if sidecar_path is not None:
        if sidecar_path.stat().st_size > MAX_INF_BYTES:
            raise AmigaFSError(
                _("INF sidecar exceeds the {limit}-byte safety limit.").format(limit=MAX_INF_BYTES)
            )
        record = parse_inf_record(sidecar_path.read_bytes())
        suggested = record.name.rsplit("/", 1)[-1].rsplit(":", 1)[-1] if record.name else ""
        return (
            suggested or source.name,
            record.metadata,
            record.length,
            _("INF sidecar {name}").format(name=sidecar_path.name),
        )
    return source.name, AmigaMeta(), None, _("neutral defaults")


def export_file(selected: str | Path, amiga_path: str, destination: str | Path) -> ExportedFile:
    """Export one image file and a matching INF without overwriting host files."""

    requested = Path(destination).expanduser()
    parent = requested.parent.resolve()
    target = parent / requested.name
    sidecar = target.with_name(f"{target.name}.inf")
    if not parent.is_dir():
        raise AmigaFSError(
            _("Export destination directory does not exist: {path}").format(path=parent)
        )
    wanted = {target.name.casefold(), sidecar.name.casefold()}
    collisions = [child for child in parent.iterdir() if child.name.casefold() in wanted]
    if collisions:
        raise AmigaFSError(
            _("Export would overwrite an existing file: {path}").format(path=collisions[0])
        )

    token = uuid.uuid4().hex
    temporary_data = parent / f".{target.name}.{token}.data"
    temporary_inf = parent / f".{target.name}.{token}.inf"
    published: list[Path] = []
    try:
        with AmigaImage.open(selected) as image:
            try:
                node = image.node_at_path(amiga_path)
            except FileNotFoundError as exc:
                raise AmigaFSError(
                    _("No such file in the image: {path}").format(path=amiga_path)
                ) from exc
            if node.is_dir:
                raise AmigaFSError(
                    _("Export currently accepts files, not directories: {path}").format(
                        path=amiga_path
                    )
                )
            metadata = image.metadata(node.inode)
            with temporary_data.open("xb") as output:
                offset = 0
                while offset < node.size:
                    data = image.read(
                        node.inode,
                        offset,
                        min(TRANSFER_CHUNK_BYTES, node.size - offset),
                    )
                    if not data:
                        raise AmigaFSError(
                            _("Image read ended at {offset} bytes; expected {size}.").format(
                                offset=offset, size=node.size
                            )
                        )
                    output.write(data)
                    offset += len(data)
                output.flush()
                os.fsync(output.fileno())
            if node.mtime_ns is not None:
                os.utime(temporary_data, ns=(node.mtime_ns, node.mtime_ns))
            with temporary_inf.open("x", encoding="latin-1", newline="") as output:
                output.write(format_inf_record(node.inner_path, node.size, metadata))
                output.flush()
                os.fsync(output.fileno())
        try:
            os.link(temporary_data, target)
            published.append(target)
            os.link(temporary_inf, sidecar)
            published.append(sidecar)
            _sync_directory(parent)
        except OSError:
            for path in published:
                path.unlink(missing_ok=True)
            _sync_directory(parent)
            raise
    except AmigaFSError:
        raise
    except Exception as exc:
        raise AmigaFSError(
            _("Could not export {path}: {error}").format(path=amiga_path, error=exc)
        ) from exc
    finally:
        temporary_data.unlink(missing_ok=True)
        temporary_inf.unlink(missing_ok=True)
    return ExportedFile(data_path=target, sidecar_path=sidecar, amiga_path=node.amiga_path)


def import_file(
    selected: str | Path,
    source_file: str | Path,
    *,
    directory: str = "",
    name: str | None = None,
    sidecar: str | Path | None = None,
    ignore_sidecar: bool = False,
) -> ImportedFile:
    """Import one host file and trusted metadata as one image mutation."""

    source = Path(source_file).expanduser().resolve()
    if not source.is_file():
        raise AmigaFSError(
            _("Import source does not exist or is not a file: {path}").format(path=source)
        )
    try:
        suggested, metadata, recorded_length, metadata_source = _import_metadata(
            source, sidecar=sidecar, ignore_sidecar=ignore_sidecar
        )
        target_name = name or suggested
        with AmigaImage.open(selected, writable=True) as image:
            try:
                parent = image.node_at_path(directory)
            except FileNotFoundError as exc:
                raise AmigaFSError(
                    _("Import destination does not exist: {path}").format(path=directory)
                ) from exc
            if not parent.is_dir:
                raise AmigaFSError(
                    _("Import destination is not a directory: {path}").format(path=directory)
                )
            if parent.volume < 0:
                raise AmigaFSError(
                    _("Name a partition to import into, for example DH0: or DH0:Tools.")
                )
            encoded = target_name.encode("utf-8")
            if image.lookup(parent.inode, encoded) is not None:
                raise AmigaFSError(
                    _("Import destination already exists: {name}").format(name=target_name)
                )
            maximum_bytes = int(image.mount_for(parent.volume).free_bytes())
            data, modified_ns = _read_stable_file(source, maximum_bytes)
            if recorded_length is not None and recorded_length != len(data):
                raise AmigaFSError(
                    _("INF length {recorded} does not match host file length {actual}.").format(
                        recorded=recorded_length, actual=len(data)
                    )
                )
            moment = min(max(datestamp_from_ns(modified_ns), AMIGA_EPOCH), amiga_now())
            node = image.import_file(
                parent.inode,
                encoded,
                data,
                AmigaMeta(
                    protection=metadata.protection, comment=metadata.comment, datestamp=moment
                ),
            )
    except AmigaFSError:
        raise
    except Exception as exc:
        raise AmigaFSError(
            _("Could not import {name}: {error}").format(name=source.name, error=exc)
        ) from exc
    return ImportedFile(node=node, source_path=source, metadata_source=metadata_source)


__all__ = [
    "ExportedFile",
    "ImportedFile",
    "InfRecord",
    "export_file",
    "format_inf_record",
    "import_file",
    "parse_inf_record",
    "parse_protection",
]
