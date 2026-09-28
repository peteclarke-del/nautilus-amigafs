"""The ``adisc`` command line and the bulk-copy machinery behind it.

Two audiences share this module. ``main`` is the command line a user or a
script drives. The underscore-prefixed helpers below it are the bulk-copy
implementation, which Amiga File Forge borrows through one adapter module so
that a directory copy preserves protection bits, comments and datestamps
without the workbench re-deriving AmigaDOS allocation policy.

Storage order matters. Writing a tree in the order the source stored it keeps
the destination's data blocks close to their file headers, which is the
difference between a hard-drive install that loads at full speed on real
hardware and one that seeks for every file.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..errors import ConfigurationError, DataError
from ..file import (
    AmigaMeta,
    format_access_text,
    parse_access_text,
    parse_protection_value,
)
from ..file.filetypes import format_filetype, parse_filetype
from ..filesystem import (
    RigidDiskMount,
    create_filesystem,
    format_volume,
    identify,
    list_filesystems,
    reader_for,
    write_geometry,
    write_rigid_disk,
)
from ..filesystem.amigados import join_path, split_path
from ..filesystem.drive import (
    allocate_image,
    create_drive,
    create_volume,
    initialise_drive,
    make_handler,
)
from ..filesystem.blocks import (
    BLOCK_SIZE,
    DD_BLOCKS,
    DOS_TYPES,
    FORMAT_LABELS,
    HD_BLOCKS,
    Geometry,
)
from ..kickfs.kickfs import Kickstart
from .mount import mount_image, resolve_mount, split_compound


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
_NUMBER_RUN = re.compile(r"(\d+)")


def _natural_name_key(name: str):
    """Sort key matching the catalogue order the shell's ``List`` presents.

    Digit runs compare numerically so ``Part2`` sorts before ``Part10``, and
    letters compare case-insensitively because AmigaDOS names are.
    """
    parts = _NUMBER_RUN.split(str(name))
    return tuple(
        (1, int(part), "") if index % 2 else (0, 0, part.casefold())
        for index, part in enumerate(parts)
    )


def _in_global_storage_order(source_mount, items: list[dict]) -> list[dict]:
    """Order copy descriptors the way the source volume stores them.

    Directories keep their relative order and always precede their contents;
    files are ordered by the block their header occupies. Reproducing the
    source's physical order in the destination is what keeps a copied tree
    compact instead of interleaved.
    """
    def key(item: dict):
        if item.get("kind") == "mkdir":
            return (0, item.get("order", 0), _natural_name_key(item.get("dst", "")))
        return (1, int(item.get("block") or 0), _natural_name_key(item.get("dst", "")))

    return sorted(items, key=key)


# ---------------------------------------------------------------------------
# Copy descriptors
# ---------------------------------------------------------------------------
def _file_item(
    source_mount, source_path: str, destination: str, *, load_data: bool = True
) -> dict:
    """Build one copy descriptor, carrying the source's catalogue metadata.

    With ``load_data`` false the descriptor carries the file's size instead of
    its bytes, and the writer reads them when it reaches the file. A tree from
    a hard-drive partition can hold gigabytes, which must not all be held in
    memory before the first file is written.
    """
    meta = (
        source_mount.amiga_meta(source_path)
        if hasattr(source_mount, "amiga_meta")
        else AmigaMeta()
    )
    filetype = None
    if hasattr(source_mount, "filetype"):
        try:
            filetype = source_mount.filetype(source_path)
        except Exception:
            filetype = None
    datestamp = None
    if hasattr(source_mount, "datestamp"):
        try:
            datestamp = source_mount.datestamp(source_path)
        except Exception:
            datestamp = None
    block = 0
    size = None
    try:
        found = source_mount.stat(source_path)
        block = int(found.block)
        size = int(found.length)
    except Exception:
        block = 0
    item = {
        "kind": "file",
        "src": source_path,
        "dst": destination,
        "size": size,
        "load": int(meta.protection) & 0xFFFFFFFF,
        "exec": 0,
        "access": int(meta.protection) & 0xFFFFFFFF,
        "comment": meta.comment,
        "filetype": filetype,
        "datestamp": datestamp,
        "block": block,
        "sourceName": source_path,
    }
    if load_data:
        item["data"] = source_mount.read_bytes(source_path)
        item["size"] = len(item["data"])
    return item


def _dir_item(destination: str, order: int) -> dict:
    return {"kind": "mkdir", "dst": destination, "order": order}


def _collect_copy_items(
    source_mount,
    source_inner: str,
    *,
    dst_mount=None,
    dst_bare: str = "",
    dst_slash: bool = False,
    recursive: bool = False,
    wildcards: bool = True,
    load_data: bool = True,
) -> list[dict]:
    """Collect every copy descriptor for one source path or wildcard.

    ``dst_slash`` says the destination was written as a directory, so a single
    source file lands *inside* it rather than replacing it. That distinction
    is the same one AmigaDOS ``Copy`` makes and it is the reason the flag is
    carried this far down.
    """
    items: list[dict] = []
    order = 0
    parts = split_path(source_inner)
    parent = join_path(parts[:-1]) if parts else ""
    leaf = parts[-1] if parts else ""

    matches: list[tuple[str, bool]] = []
    if wildcards and leaf and any(character in leaf for character in "*?"):
        for entry in source_mount.iter_entries(parent):
            if fnmatch.fnmatch(entry.name.casefold(), leaf.casefold()):
                matches.append((entry.path, entry.is_dir))
    else:
        stat = source_mount.stat(source_inner)
        matches.append((join_path(parts), stat.is_dir))

    if not matches:
        raise DataError(f"Nothing matched {source_inner}.")

    multiple = len(matches) > 1
    for source_path, is_dir in sorted(matches, key=lambda row: _natural_name_key(row[0])):
        name = split_path(source_path)[-1] if split_path(source_path) else ""
        # A source lands *inside* the destination when the destination already
        # exists as a directory, was written with a trailing separator, or is
        # receiving more than one match. Otherwise the destination names the
        # copy itself, which is what ``Copy`` does on a real machine.
        into_directory = (
            dst_slash
            or multiple
            or (
                dst_mount is not None
                and dst_bare
                and dst_mount.exists(dst_bare)
                and dst_mount.stat(dst_bare).is_dir
            )
        )
        if into_directory and name:
            destination = join_path([*split_path(dst_bare), name])
        else:
            destination = dst_bare or name
        if not is_dir:
            items.append(
                _file_item(source_mount, source_path, destination, load_data=load_data)
            )
            continue
        if not recursive:
            raise ConfigurationError(
                f"{source_path} is a directory. Use --recursive to copy its contents."
            )
        items.append(_dir_item(destination, order))
        order += 1
        stack = [(source_path, destination)]
        while stack:
            current_source, current_destination = stack.pop(0)
            for entry in sorted(
                source_mount.iter_entries(current_source),
                key=lambda entry: _natural_name_key(entry.name),
            ):
                child_destination = join_path(
                    [*split_path(current_destination), entry.name]
                )
                if entry.is_dir:
                    items.append(_dir_item(child_destination, order))
                    order += 1
                    stack.append((entry.path, child_destination))
                else:
                    items.append(
                        _file_item(
                            source_mount, entry.path, child_destination, load_data=load_data
                        )
                    )
    return items


def _ensure_dir_chain(mount, path: str) -> None:
    """Create every missing directory above and including ``path``."""
    parts = split_path(path)
    if not parts:
        return
    for depth in range(1, len(parts) + 1):
        branch = join_path(parts[:depth])
        if not mount.exists(branch):
            mount.mkdir(branch)
        elif not mount.stat(branch).is_dir:
            raise DataError(f"{branch} already exists and is not a directory.")


def _write_copy_item(mount, destination: str, item: dict, overwrite: bool) -> None:
    """Write one copy descriptor into a mounted volume."""
    if mount.exists(destination):
        if not overwrite:
            raise DataError(f"{destination} already exists.")
        mount.remove(destination, force=True)
    _ensure_dir_chain(mount, join_path(split_path(destination)[:-1]))
    protection = int(item.get("access") or item.get("load") or 0) & 0xFFFFFFFF
    meta = AmigaMeta(
        protection=protection,
        comment=str(item.get("comment") or ""),
        datestamp=item.get("datestamp"),
    )
    mount.write_bytes(destination, item["data"], meta)
    filetype = item.get("filetype")
    if filetype is not None and hasattr(mount, "set_filetype"):
        mount.set_filetype(destination, filetype)
    datestamp = item.get("datestamp")
    if datestamp is not None and hasattr(mount, "set_datestamp"):
        mount.set_datestamp(destination, datestamp)


def _walk_post_order_mount(mount, path: str) -> list[str]:
    """List a tree children-first, so directories can be removed after them."""
    if not mount.exists(path):
        return []
    if not mount.stat(path).is_dir:
        return [join_path(split_path(path))]
    collected: list[str] = []
    for entry in mount.iter_entries(path):
        if entry.is_dir:
            collected.extend(_walk_post_order_mount(mount, entry.path))
        else:
            collected.append(entry.path)
    collected.append(join_path(split_path(path)))
    return collected


# ---------------------------------------------------------------------------
# Report helpers used by the JSON output
# ---------------------------------------------------------------------------
def _entry_rows(mount, inner: str) -> list[dict]:
    rows: list[dict] = []
    for entry in sorted(mount.iter_entries(inner), key=lambda item: _natural_name_key(item.name)):
        if entry.is_dir:
            rows.append(
                {
                    "name": entry.name,
                    "type": "dir",
                    "load": "",
                    "exec": "",
                    "filetype": "",
                    "datestamp": "",
                    "length": sum(1 for _child in mount.iter_entries(entry.path)),
                    "attr": "",
                }
            )
            continue
        meta = mount.amiga_meta(entry.path) if hasattr(mount, "amiga_meta") else AmigaMeta()
        filetype = ""
        if hasattr(mount, "filetype"):
            value = mount.filetype(entry.path)
            if value is not None:
                filetype = int(value)
        rows.append(
            {
                "name": entry.name,
                "type": "file",
                "load": int(meta.protection) & 0xFFFFFFFF,
                "exec": 0,
                "filetype": filetype,
                "datestamp": meta.datestamp.isoformat(sep="T", timespec="milliseconds")
                if meta.datestamp
                else "",
                "length": entry.length,
                "attr": format_access_text(meta.access),
                "comment": meta.comment,
            }
        )
    return rows


def _report(rows: list[dict], **metadata) -> dict:
    return {"rows": rows, "metadata": metadata}


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def _dos_type_for(name: str) -> bytes:
    key = str(name or "FFS-INTL").strip().upper()
    if key in FORMAT_LABELS:
        return FORMAT_LABELS[key]
    aliases = {
        "OFS": b"DOS\x00",
        "FFS": b"DOS\x01",
        "AMIGADOS": b"DOS\x03",
        "DOS0": b"DOS\x00",
        "DOS1": b"DOS\x01",
        "DOS2": b"DOS\x02",
        "DOS3": b"DOS\x03",
        "DOS4": b"DOS\x04",
        "DOS5": b"DOS\x05",
        "DOS6": b"DOS\x06",
        "DOS7": b"DOS\x07",
    }
    if key in aliases:
        return aliases[key]
    raise ConfigurationError(
        f"{name!r} is not a filing-system variant. Choose one of: "
        + ", ".join(sorted(FORMAT_LABELS))
    )


def _geometry_for(text: str) -> tuple[int, Geometry | None]:
    """Return the block count and geometry for a named or sized request."""
    request = str(text or "dd").strip().lower()
    if request in {"dd", "880k", "floppy"}:
        return DD_BLOCKS, None
    if request in {"hd", "1760k", "1.76m"}:
        return HD_BLOCKS, None
    match = re.fullmatch(r"(?:capacity=)?([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?)i?b?", request)
    if match:
        # A size is counted in powers of 1024 whichever way it is spelt, as
        # AmigaDOS and HDToolBox count: 128GB and 128GiB are the same drive.
        scale = 1024 ** " kmgt".index(match.group(2) or " ")
        total_bytes = int(float(match.group(1)) * scale)
        blocks = total_bytes // BLOCK_SIZE
        if blocks < 32:
            raise ConfigurationError("A volume needs at least 16 KiB.")
        return blocks, None
    raise ConfigurationError(
        f"{text!r} is not a geometry. Use dd, hd or capacity=<size>."
    )


def command_identify(args) -> int:
    path = Path(args.image)
    rows = [candidate.to_dict() for candidate in identify(path, suffix_hint=path.suffix.lower())]
    if args.output_format == "json":
        _emit({"reports": {"candidates": _report(rows, title=path.name)}})
        return 0
    if not rows:
        raise DataError(f"No supported filing system was found in {path.name}.")
    for row in rows:
        print(f"{row['filesystem']:<10} {row['confidence']:<6} {row['detail']}")
    return 0


def command_create(args) -> int:
    path = Path(args.image)
    if args.filesystem == "kickfs":
        from ..kickfs.kickfs import SIZE_256K, SIZE_512K, build_rom

        sizes = {"256k": SIZE_256K, "512k": SIZE_512K, "1m": 2 * SIZE_512K}
        request = str(args.geometry or "256k").strip().lower().replace("ib", "")
        if request not in sizes:
            raise ConfigurationError("A ROM image is 256k, 512k or 1m.")
        label = str(args.title or "forge").strip() or "forge"
        path.write_bytes(
            build_rom(
                size=sizes[request],
                name=f"{label}.library",
                id_string=f"{label}.library 1.0 (2026)",
            )
        )
        return 0
    if args.layout:
        return _create_from_layout(path, Path(args.layout))
    blocks, geometry = _geometry_for(args.geometry or "dd")
    if str(args.filesystem or "").lower() in ("pfs3", "pds3", "sfs"):
        create_volume(
            path, blocks * BLOCK_SIZE, args.filesystem, args.title or "Empty",
            bootable=bool(args.bootable),
        )
        return 0
    if args.filesystem == "rdb":
        size = blocks * BLOCK_SIZE
        allocate_image(path, size)
        reader = reader_for(path, writable=True)
        try:
            partitions = []
            count = max(1, int(args.partitions or 1))
            for index in range(count):
                partitions.append(
                    {
                        "name": f"DH{index}",
                        "dosType": _dos_type_for(args.variant or "FFS-INTL"),
                        "cylinders": 0,
                        "sizeBytes": size // count,
                        "bootable": index == 0,
                    }
                )
            disk = write_rigid_disk(reader, partitions)
            for partition in disk.partitions:
                window = reader.window(partition.start_block, partition.total_blocks)
                format_volume(
                    window,
                    label=partition.name,
                    dos_type=partition.dos_type,
                    bootable=partition.bootable,
                    geometry=partition.geometry(),
                )
                window.close()
        finally:
            reader.close()
        if args.geometry_sidecar:
            Path(str(path) + ".geo").write_text(write_geometry(Geometry()))
        return 0

    allocate_image(path, blocks * BLOCK_SIZE)
    dos_type = _dos_type_for(args.variant or args.filesystem or "OFS")
    reader = reader_for(path, writable=True)
    try:
        format_volume(
            reader,
            label=args.title or "Empty",
            dos_type=dos_type,
            bootable=bool(args.bootable),
            geometry=geometry,
        )
    finally:
        reader.close()
    if args.geometry_sidecar:
        # A hardfile carries no partition table, so the host has to be told
        # its shape. Choose surfaces and sectors that divide the block count
        # exactly, so the declared capacity and the file agree to the byte.
        surfaces, sectors, cylinders = _hardfile_shape(blocks)
        Path(str(path) + ".geo").write_text(
            write_geometry(
                Geometry(
                    surfaces=surfaces,
                    blocks_per_track=sectors,
                    high_cylinder=cylinders - 1,
                    dos_type=dos_type,
                )
            )
        )
    return 0


def _create_from_layout(path: Path, layout_path: Path) -> int:
    """Create a partitioned drive from a layout written as JSON.

    The layout names the drive's size, its partitions and the handler files
    to embed::

        {"size": "128GB",
         "partitions": [
           {"name": "DH0", "label": "System", "filesystem": "pfs3",
            "size": "2GB", "bootable": true},
           {"name": "DH1", "label": "Work", "filesystem": "pfs3"}],
         "handlers": [{"filesystem": "pfs3", "path": "pfs3aio"}]}

    A partition with no size takes what the others leave.
    """
    try:
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigurationError(f"The layout could not be read: {error}") from error
    if not isinstance(layout, dict) or not isinstance(layout.get("partitions"), list):
        raise ConfigurationError("A layout names a size and a list of partitions.")

    def size_of(value) -> int:
        if value in (None, "", 0):
            return 0
        if isinstance(value, (int, float)):
            return int(value)
        return _geometry_for(str(value))[0] * BLOCK_SIZE

    partitions = []
    for spec in layout["partitions"]:
        entry = dict(spec)
        entry["sizeBytes"] = size_of(spec.get("sizeBytes", spec.get("size")))
        partitions.append(entry)
    handlers = []
    for spec in layout.get("handlers") or []:
        source = Path(str(spec.get("path") or ""))
        if not source.is_absolute():
            source = layout_path.parent / source
        try:
            binary = source.read_bytes()
        except OSError as error:
            raise ConfigurationError(f"The handler {source.name} could not be read.") from error
        handlers.append(make_handler(spec.get("filesystem") or spec.get("dosType"), binary))
    size = size_of(layout.get("sizeBytes", layout.get("size")))
    if path.exists() and not size:
        initialise_drive(path, partitions, handlers=handlers)
    else:
        create_drive(path, size, partitions, handlers=handlers)
    return 0


def _hardfile_shape(blocks: int) -> tuple[int, int, int]:
    """Choose surfaces, sectors and cylinders that multiply to exactly ``blocks``.

    An emulator multiplies the three numbers back out and refuses a hardfile
    whose file size does not match, so an approximate shape is worse than
    none. Preferred values are tried first and the search falls back to a
    single-surface, single-sector geometry, which always divides.
    """
    for surfaces in (16, 8, 4, 2, 1):
        if blocks % surfaces:
            continue
        remaining = blocks // surfaces
        for sectors in (63, 32, 17, 11, 1):
            if remaining % sectors == 0:
                return surfaces, sectors, remaining // sectors
    return 1, 1, blocks


def command_ls(args) -> int:
    with resolve_mount(args.path) as resolved:
        mount = resolved.mount
        inner = resolved.path
        if isinstance(mount, RigidDiskMount):
            rows = [
                {
                    "name": partition.name,
                    "type": "dir",
                    "load": "",
                    "exec": "",
                    "filetype": "",
                    "datestamp": "",
                    "length": partition.total_blocks,
                    "attr": "",
                    "format": partition.format,
                    "bootable": partition.bootable,
                }
                for partition in mount.partitions
            ]
            metadata = {
                "title": resolved.image.name,
                "description": f"{len(rows)} RDB partition{'s' if len(rows) != 1 else ''}",
                "path": inner,
            }
        else:
            if not mount.exists(inner):
                raise DataError(f"Path not found: {inner or ':'}")
            if not mount.stat(inner).is_dir:
                raise DataError(f"{inner} is not a directory.")
            rows = _entry_rows(mount, inner)
            files = sum(1 for row in rows if row["type"] == "file")
            used = sum(int(row["length"]) for row in rows if row["type"] == "file")
            metadata = {
                "title": mount.title,
                "description": f"{files} file(s), {used:,} bytes",
                "path": inner,
                "format": getattr(mount, "format", ""),
            }
        if args.output_format == "json":
            _emit({"reports": {"entries": _report(rows, **metadata)}})
            return 0
        print(f"Directory \"{metadata['title']}:{inner}\"")
        for row in rows:
            if row["type"] == "dir":
                print(f"{row['name']:<32} Dir")
            else:
                print(f"{row['name']:<32}{row['length']:>10} {row['attr']}")
        print(metadata["description"])
    return 0


def command_stat(args) -> int:
    image, inner = split_compound(args.path)
    mount, name = mount_image(image)
    try:
        if inner:
            entry = mount.stat(inner)
            rows = [
                {
                    "name": entry.name,
                    "path": entry.path,
                    "type": "dir" if entry.is_dir else "file",
                    "length": entry.length,
                    "blocks": entry.blocks,
                }
            ]
            payload = {"reports": {"entry": _report(rows, title=mount.title)}}
        elif isinstance(mount, RigidDiskMount):
            rows = []
            for partition in mount.partitions:
                volume = None
                try:
                    volume = mount.open_partition(partition.index)
                    rows.append(
                        {
                            "name": partition.name,
                            "format": partition.format,
                            "size": volume.size_bytes(),
                            "free": volume.free_bytes(),
                            "bootable": partition.bootable,
                        }
                    )
                except DataError:
                    rows.append(
                        {
                            "name": partition.name,
                            "format": partition.format,
                            "size": partition.size_bytes,
                            "free": 0,
                            "bootable": partition.bootable,
                            "note": "unformatted",
                        }
                    )
                finally:
                    if volume is not None:
                        volume.close()
            payload = {
                "reports": {"partitions": _report(rows, title=image.name)},
                "description": f"{len(rows)} RDB partition(s)",
            }
        else:
            rows = [
                {
                    "name": mount.title,
                    "format": getattr(mount, "format", name),
                    "size": mount.size_bytes(),
                    "free": mount.free_bytes(),
                }
            ]
            payload = {
                "reports": {"volume": _report(rows, title=mount.title)},
                "description": f"{getattr(mount, 'format', name)} volume",
            }
        if args.output_format == "json":
            _emit(payload)
            return 0
        for report in payload["reports"].values():
            for row in report["rows"]:
                print(" ".join(f"{key}={value}" for key, value in row.items()))
    finally:
        close = getattr(mount, "close", None)
        if callable(close):
            close()
    return 0


def command_validate(args) -> int:
    image, _inner = split_compound(args.image)
    mount, _name = mount_image(image)
    try:
        problems = mount.validate() if hasattr(mount, "validate") else []
        if isinstance(mount, RigidDiskMount):
            problems = []
            for partition in mount.partitions:
                try:
                    volume = mount.open_partition(partition.index)
                except DataError as error:
                    problems.append(f"{partition.name}: {error}")
                    continue
                problems.extend(
                    f"{partition.name}: {problem}" for problem in volume.validate()
                )
                volume.close()
    finally:
        close = getattr(mount, "close", None)
        if callable(close):
            close()
    if problems:
        raise DataError("; ".join(problems))
    print("No structural errors found")
    return 0


def command_get(args) -> int:
    with resolve_mount(args.path) as resolved:
        data = resolved.mount.read_bytes(resolved.path)
    if args.destination == "-":
        sys.stdout.buffer.write(data)
    else:
        Path(args.destination).write_bytes(data)
    return 0


def command_put(args) -> int:
    data = sys.stdin.buffer.read() if args.source == "-" else Path(args.source).read_bytes()
    with resolve_mount(args.path, writable=True) as resolved:
        mount = resolved.mount
        _ensure_dir_chain(mount, join_path(split_path(resolved.path)[:-1]))
        meta = AmigaMeta(
            protection=parse_protection_value(args.protection) if args.protection else 0,
            comment=args.comment or "",
        )
        if mount.exists(resolved.path):
            mount.remove(resolved.path, force=True)
        mount.write_bytes(resolved.path, data, meta)
        if args.filetype:
            mount.set_filetype(resolved.path, parse_filetype(args.filetype))
        mount.flush()
    return 0


def command_cp(args) -> int:
    source_image, source_inner = split_compound(args.source)
    target_image, target_inner = split_compound(args.destination)
    destination_slash = args.destination.endswith(("/", ":"))
    source_mount, _ = mount_image(source_image)
    try:
        if source_image == target_image:
            target_mount, _ = mount_image(target_image, writable=True)
            source_mount.close()
            source_mount = target_mount
        else:
            target_mount, _ = mount_image(target_image, writable=True)
        try:
            items = _collect_copy_items(
                source_mount,
                source_inner,
                dst_mount=target_mount,
                dst_bare=join_path(split_path(target_inner)),
                dst_slash=destination_slash,
                recursive=bool(args.recursive),
                wildcards=not args.no_wildcards,
            )
            for item in _in_global_storage_order(source_mount, items):
                if item["kind"] == "mkdir":
                    _ensure_dir_chain(target_mount, item["dst"])
                else:
                    _write_copy_item(target_mount, item["dst"], item, bool(args.force))
            target_mount.flush()
        finally:
            if target_mount is not source_mount:
                target_mount.close()
    finally:
        source_mount.close()
    return 0


def command_mv(args) -> int:
    """Rename or move an entry inside one image.

    The destination is an inner path, not a second compound path: a move
    between two images is a copy followed by a delete, which is what ``cp``
    and ``rm`` are for. Accepting a compound destination here would let a
    caller silently believe it had moved data between volumes.
    """
    _image, source_inner = split_compound(args.source)
    destination = args.destination
    if ":" in destination:
        _target_image, destination = split_compound(destination)
    with resolve_mount(args.source, writable=True) as resolved:
        resolved.mount.rename(source_inner, destination)
        resolved.mount.flush()
    return 0


def command_rm(args) -> int:
    """Delete one or several entries from the same image in one open.

    A multiple selection is one operation to the user, so it is one operation
    here: the image is opened once, every path is checked, and the deletions
    happen together.
    """
    first, *rest = args.paths
    image, _inner = split_compound(first)
    inners = [split_compound(path)[1] for path in args.paths]
    del rest
    with resolve_mount(str(image), writable=True) as resolved:
        mount = resolved.mount
        for inner in inners:
            targets = (
                _walk_post_order_mount(mount, inner) if args.recursive else [inner]
            )
            for path in targets:
                mount.remove(path, force=bool(args.force), recursive=False)
        mount.flush()
    return 0


def command_mkdir(args) -> int:
    with resolve_mount(args.path, writable=True) as resolved:
        _ensure_dir_chain(resolved.mount, resolved.path)
        resolved.mount.flush()
    return 0


def command_opt(args) -> int:
    with resolve_mount(args.image, writable=args.option is not None) as resolved:
        mount = resolved.mount
        if args.option is None:
            print(mount.boot_option())
            return 0
        mount.set_boot_option(int(args.option))
        mount.flush()
    return 0


def command_title(args) -> int:
    with resolve_mount(args.path, writable=args.title is not None) as resolved:
        node = resolved.mount._navigate(resolved.path)
        if args.title is None:
            print(node.title)
            return 0
        node.set_title(args.title)
        resolved.mount.flush()
    return 0


def command_chmod(args) -> int:
    with resolve_mount(args.path, writable=True) as resolved:
        resolved.mount.set_access(resolved.path, parse_access_text(args.flags))
        resolved.mount.flush()
    return 0


def command_lock(args) -> int:
    with resolve_mount(args.path, writable=True) as resolved:
        access = resolved.mount.access(resolved.path)
        resolved.mount.set_access(resolved.path, access.with_locked(True))
        resolved.mount.flush()
    return 0


def command_unlock(args) -> int:
    with resolve_mount(args.path, writable=True) as resolved:
        access = resolved.mount.access(resolved.path)
        resolved.mount.set_access(resolved.path, access.with_locked(False))
        resolved.mount.flush()
    return 0


def command_compact(args) -> int:
    with resolve_mount(args.image, writable=True) as resolved:
        moved = resolved.mount.defragment()
        resolved.mount.flush()
    print(f"{moved} file(s) rewritten contiguously")
    return 0


def command_tree(args) -> int:
    with resolve_mount(args.path) as resolved:
        mount = resolved.mount

        def walk(path: str, depth: int) -> None:
            for entry in sorted(mount.iter_entries(path), key=lambda item: _natural_name_key(item.name)):
                marker = "/" if entry.is_dir else ""
                print(f"{'  ' * depth}{entry.name}{marker}")
                if entry.is_dir:
                    walk(entry.path, depth + 1)

        walk(resolved.path, 0)
    return 0


def command_find(args) -> int:
    with resolve_mount(args.path) as resolved:
        mount = resolved.mount
        pattern = args.pattern.casefold()

        def walk(path: str) -> None:
            for entry in mount.iter_entries(path):
                if fnmatch.fnmatch(entry.name.casefold(), pattern):
                    print(entry.path)
                if entry.is_dir:
                    walk(entry.path)

        walk(resolved.path)
    return 0


def command_freemap(args) -> int:
    with resolve_mount(args.image) as resolved:
        flags = resolved.mount.free_map()
        width = 64
        for start in range(0, len(flags), width):
            row = flags[start : start + width]
            print(f"{start:>8} " + "".join("." if free else "#" for free in row))
        print(f"{sum(flags):,} free of {len(flags):,} blocks")
    return 0


def command_export(args) -> int:
    destination = Path(args.destination)
    destination.mkdir(parents=True, exist_ok=True)
    with resolve_mount(args.path) as resolved:
        mount = resolved.mount

        def walk(path: str, target: Path) -> None:
            target.mkdir(parents=True, exist_ok=True)
            for entry in mount.iter_entries(path):
                if entry.is_dir:
                    walk(entry.path, target / entry.name)
                else:
                    (target / entry.name).write_bytes(mount.read_bytes(entry.path))

        walk(resolved.path, destination)
    return 0


def command_import(args) -> int:
    source = Path(args.source)
    if not source.is_dir():
        raise ConfigurationError(f"{source} is not a directory.")
    with resolve_mount(args.path, writable=True) as resolved:
        mount = resolved.mount
        for item in sorted(source.rglob("*")):
            relative = item.relative_to(source)
            inner = join_path([*split_path(resolved.path), *relative.parts])
            if item.is_dir():
                _ensure_dir_chain(mount, inner)
            else:
                _ensure_dir_chain(mount, join_path(split_path(inner)[:-1]))
                if mount.exists(inner):
                    mount.remove(inner, force=True)
                mount.write_bytes(inner, item.read_bytes())
        mount.flush()
    return 0


def command_cat(args) -> int:
    with resolve_mount(args.path) as resolved:
        sys.stdout.buffer.write(resolved.mount.read_bytes(resolved.path))
    return 0


def command_type(args) -> int:
    with resolve_mount(args.path) as resolved:
        data = resolved.mount.read_bytes(resolved.path)
    sys.stdout.write(data.decode("latin-1").replace("\r\n", "\n").replace("\r", "\n"))
    return 0


def command_get_datestamp(args) -> int:
    with resolve_mount(args.path) as resolved:
        stamp = resolved.mount.datestamp(resolved.path)
    print(stamp.isoformat(sep="T", timespec="milliseconds") if stamp else "")
    return 0


def command_set_datestamp(args) -> int:
    moment = (
        datetime.now(timezone.utc)
        if args.value in {"now", None}
        else datetime.fromisoformat(args.value)
    )
    with resolve_mount(args.path, writable=True) as resolved:
        resolved.mount.set_datestamp(resolved.path, moment)
        resolved.mount.flush()
    return 0


def command_get_filetype(args) -> int:
    with resolve_mount(args.path) as resolved:
        print(format_filetype(resolved.mount.filetype(resolved.path)))
    return 0


def command_set_filetype(args) -> int:
    with resolve_mount(args.path, writable=True) as resolved:
        resolved.mount.set_filetype(resolved.path, args.value)
        resolved.mount.flush()
    return 0


def command_storage_order(args) -> int:
    with resolve_mount(args.path) as resolved:
        mount = resolved.mount
        rows = []

        def walk(path: str) -> None:
            for entry in mount.iter_entries(path):
                if entry.is_dir:
                    walk(entry.path)
                else:
                    rows.append((entry.block, entry.path, entry.length))

        walk(resolved.path)
        for block, path, length in sorted(rows):
            print(f"{block:>8} {length:>10} {path}")
    return 0


def command_list_filesystems(args) -> int:
    for row in list_filesystems():
        print(f"{row['name']:<10} {row['label']}")
    return 0


def command_describe_filesystem(args) -> int:
    driver = create_filesystem(args.name)
    variants = ", ".join(sorted(DOS_TYPES.values()))
    print(f"{driver.name}: {driver.label}")
    print(f"Recognised DOS types: {variants}")
    return 0


def command_kickstart(args) -> int:
    rom = Kickstart(Path(args.image).read_bytes())
    if args.output_format == "json":
        _emit(rom.to_dict())
        return 0
    print(f"{rom.release} (exec {rom.version}), {len(rom.data) // 1024} KiB")
    print(f"Checksum {'valid' if rom.checksum_valid else 'INVALID'}")
    for module in rom.modules:
        print(
            f"  {module.name:<24} v{module.version:<4} pri {module.priority:>4} "
            f"{module.length:>8} bytes  {module.id_string}"
        )
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adisc",
        description="Work with AmigaDOS OFS and FFS volumes, RDB hard drives and Kickstart ROMs.",
    )
    parser.add_argument("--version", action="store_true", help="Show the engine version and exit.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Re-raise engine errors after printing them, so the traceback is visible.",
    )
    commands = parser.add_subparsers(dest="command")

    def add(name, handler, help_text):
        sub = commands.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        return sub

    sub = add("identify", command_identify, "Identify an image's filing system by content.")
    sub.add_argument("--as", dest="output_format", default="text", choices=("text", "json"))
    sub.add_argument("image")

    sub = add("create", command_create, "Create a new empty image.")
    sub.add_argument("--filesystem", default="ofs")
    sub.add_argument("--variant", default=None, help="OFS, FFS, OFS-INTL, FFS-INTL, OFS-DC, FFS-DC, OFS-LNFS or FFS-LNFS.")
    sub.add_argument("--geometry", default="dd")
    sub.add_argument("--title", default="Empty")
    sub.add_argument("--partitions", type=int, default=1)
    sub.add_argument("--bootable", action="store_true")
    sub.add_argument("--geometry-sidecar", action="store_true")
    sub.add_argument(
        "--layout",
        default=None,
        help="A JSON file naming the drive's size, partitions and handlers.",
    )
    sub.add_argument("image")

    sub = add("ls", command_ls, "List directory contents.")
    sub.add_argument("--as", dest="output_format", default="text", choices=("text", "json"))
    sub.add_argument("path")

    sub = add("stat", command_stat, "Volume summary, or metadata for one path.")
    sub.add_argument("--as", dest="output_format", default="text", choices=("text", "json"))
    sub.add_argument("path")

    sub = add("validate", command_validate, "Check an image's structure for inconsistencies.")
    sub.add_argument("image")

    sub = add("get", command_get, "Export a file from an image to the host.")
    sub.add_argument("--meta-format", default="none")
    sub.add_argument("path")
    sub.add_argument("destination")

    sub = add("put", command_put, "Import a host file into an image.")
    sub.add_argument(
        "--protection",
        default=None,
        help="Protection long, as decimal or &hex. Its low four bits are inverted.",
    )
    sub.add_argument("--comment", default=None)
    sub.add_argument("--filetype", default=None)
    sub.add_argument("source")
    sub.add_argument("path")

    sub = add("cp", command_cp, "Copy files or a tree within or between images.")
    sub.add_argument("--recursive", action="store_true")
    sub.add_argument("--force", action="store_true")
    sub.add_argument("--no-wildcards", action="store_true")
    sub.add_argument("--order", default=None)
    sub.add_argument("source")
    sub.add_argument("destination")

    sub = add("mv", command_mv, "Rename or move an entry inside an image.")
    sub.add_argument("source")
    sub.add_argument("destination")

    sub = add("rm", command_rm, "Delete entries from an image.")
    sub.add_argument("--recursive", action="store_true")
    sub.add_argument("--force", action="store_true")
    sub.add_argument("paths", nargs="+")

    sub = add("mkdir", command_mkdir, "Create a directory.")
    sub.add_argument("path")

    sub = add("opt", command_opt, "Read or set the bootblock option.")
    sub.add_argument("image")
    sub.add_argument("option", nargs="?", default=None)

    sub = add("title", command_title, "Read or set a volume or drawer title.")
    sub.add_argument("path")
    sub.add_argument("title", nargs="?", default=None)

    sub = add("chmod", command_chmod, "Set protection flags (AmigaDOS alias: Protect).")
    sub.add_argument("path")
    sub.add_argument("flags")

    sub = add("lock", command_lock, "Protect an entry against deletion and writing.")
    sub.add_argument("path")

    sub = add("unlock", command_unlock, "Remove delete and write protection.")
    sub.add_argument("path")

    sub = add("compact", command_compact, "Rewrite files so their blocks are contiguous.")
    sub.add_argument("image")

    sub = add("tree", command_tree, "Display a recursive directory tree.")
    sub.add_argument("path")

    sub = add("find", command_find, "Find entries matching a pattern.")
    sub.add_argument("path")
    sub.add_argument("pattern")

    sub = add("freemap", command_freemap, "Show the block-allocation bitmap.")
    sub.add_argument("image")

    sub = add("export", command_export, "Bulk-export an image to a host directory.")
    sub.add_argument("path")
    sub.add_argument("destination")

    sub = add("import", command_import, "Bulk-import a host directory into an image.")
    sub.add_argument("source")
    sub.add_argument("path")

    sub = add("cat", command_cat, "Write a file's raw bytes to standard output.")
    sub.add_argument("path")

    sub = add("type", command_type, "Display a text file with host line endings.")
    sub.add_argument("path")

    sub = add("get-datestamp", command_get_datestamp, "Print an entry's datestamp.")
    sub.add_argument("path")

    sub = add("set-datestamp", command_set_datestamp, "Set an entry's datestamp.")
    sub.add_argument("path")
    sub.add_argument("value", nargs="?", default="now")

    sub = add("get-filetype", command_get_filetype, "Print an entry's Workbench type.")
    sub.add_argument("path")

    sub = add("set-filetype", command_set_filetype, "Set an entry's Workbench type.")
    sub.add_argument("path")
    sub.add_argument("value")

    sub = add("storage-order", command_storage_order, "List files in physical storage order.")
    sub.add_argument("path")

    add("list-filesystems", command_list_filesystems, "List the filing systems this build recognises.")

    sub = add("describe-filesystem", command_describe_filesystem, "Describe one filing system.")
    sub.add_argument("name")

    sub = add("kickstart", command_kickstart, "Decode a Kickstart ROM's resident modules.")
    sub.add_argument("--as", dest="output_format", default="text", choices=("text", "json"))
    sub.add_argument("image")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        from ..version import __version__

        print(__version__)
        return 0
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args)
    except (ConfigurationError, DataError) as error:
        sys.stderr.write(f"Error: {error}\n")
        if args.debug:
            raise
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
