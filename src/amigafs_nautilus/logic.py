"""Pure selection logic for the Nautilus extension."""

from __future__ import annotations

import os
from pathlib import Path

from amigafs.core import ImageCapabilities, ImageProperties, resolve_image
from amigafs.core.formats import ResolvedImage, image_capabilities_hint
from amigafs.core.properties import kind_label
from amigafs.errors import AmigaFSError
from amigafs.i18n import _, ngettext

#: A container this large is described from its header alone, because decoding
#: it would stall the file manager.
MAX_PROPERTIES_DECODE_BYTES = 8 * 1024 * 1024


def image_capabilities(path: str | Path) -> ImageCapabilities | None:
    try:
        return resolve_image(path).capabilities
    except (AmigaFSError, OSError):
        return None


def menu_capabilities(path: str | Path) -> ImageCapabilities | None:
    """Return capabilities for a file the desktop claims, confirmed by its header.

    The suffix is checked first so unrelated files cost nothing. The header is
    then read so an Acorn ``.adf`` or a PC ``.rom`` is not offered Amiga actions.
    """

    if image_capabilities_hint(path) is None:
        return None
    return image_capabilities(path)


def is_supported_image(path: str | Path) -> bool:
    return menu_capabilities(path) is not None


def decodes_quickly(source: ResolvedImage) -> bool:
    """Return whether full properties can be read without stalling the desktop."""

    if source.container is None:
        return True
    if source.container == "greaseweazle":
        return False
    try:
        return source.primary_path.stat().st_size <= MAX_PROPERTIES_DECODE_BYTES
    except OSError:
        return False


def _size(value: int | None) -> str:
    if value is None:
        return "—"
    units = (_("bytes"), _("KiB"), _("MiB"), _("GiB"))
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{int(amount)} {unit}" if unit == _("bytes") else f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def summary_property_rows(source: ResolvedImage) -> tuple[tuple[str, str], ...]:
    """Describe a source from its header, without decoding it."""

    rows = [(_("Image type"), kind_label(source.kind))]
    if source.container:
        rows.append((_("Container"), source.container.upper()))
    rows.append(
        (
            _("Read-write mounting"),
            _("Supported") if source.capabilities.mount_read_write else _("Read-only format"),
        )
    )
    rows.append((_("Details"), _("Open the image to see its volumes.")))
    return tuple(rows)


def image_property_rows(properties: ImageProperties) -> tuple[tuple[str, str], ...]:
    """Convert typed core metadata to stable labels for Nautilus."""

    rows = [(_("Image type"), properties.image_type)]
    if properties.container:
        rows.append((_("Container"), properties.container))
    rows.append((_("Layout"), properties.layout_label))
    if properties.disc_product:
        identity = " ".join(
            part
            for part in (properties.disc_vendor, properties.disc_product, properties.disc_revision)
            if part
        )
        rows.append((_("Drive identity"), identity))
    if properties.cylinders is not None:
        rows.append(
            (
                _("Geometry"),
                _("{cylinders} cylinders × {heads} heads × {sectors} sectors/track").format(
                    cylinders=properties.cylinders,
                    heads=properties.heads,
                    sectors=properties.sectors_per_track,
                ),
            )
        )
    rows.append((_("Capacity"), _size(properties.capacity_bytes)))
    single = len(properties.volumes) == 1
    for volume in properties.volumes:
        prefix = "" if single else f"{volume.name}: "
        filesystem = f"{volume.format} ({volume.dos_type})" if volume.dos_type else volume.format
        rows.append((prefix + _("Filesystem"), filesystem))
        if not volume.mounted:
            rows.append((prefix + _("Status"), volume.problem or _("Not mounted")))
            continue
        rows.append((prefix + _("Volume name"), volume.title or "—"))
        if volume.first_cylinder is not None:
            rows.append(
                (
                    prefix + _("Cylinders"),
                    f"{volume.first_cylinder}–{volume.last_cylinder}",
                )
            )
            if volume.bootable:
                rows.append(
                    (
                        prefix + _("Boot priority"),
                        str(volume.boot_priority),
                    )
                )
        rows.append((prefix + _("Size"), _size(volume.capacity_bytes)))
        if volume.free_bytes is not None:
            rows.append((prefix + _("Used"), _size(volume.used_bytes)))
            rows.append((prefix + _("Free"), _size(volume.free_bytes)))
        contents = _("{files}, {drawers}").format(
            files=ngettext("{count} file", "{count} files", volume.files).format(
                count=volume.files
            ),
            drawers=ngettext("{count} drawer", "{count} drawers", volume.directories).format(
                count=volume.directories
            ),
        )
        rows.append((prefix + _("Contents"), contents))
    if not properties.read_write_supported:
        validation = _("Supported read-only")
    elif properties.fatal_findings:
        validation = _("Unsafe for read-write mounting")
    else:
        validation = _("Safe for read-write mounting")
    if properties.fatal_findings or properties.warning_findings or properties.advice_findings:
        problems = properties.fatal_findings + properties.warning_findings
        problem_text = ngettext("{count} problem", "{count} problems", problems).format(
            count=problems
        )
        advice = properties.advice_findings
        advice_text = ngettext("{count} advice", "{count} advice", advice).format(count=advice)
        validation += _(" ({problems}, {advice})").format(
            problems=problem_text,
            advice=advice_text,
        )
    rows.append((_("Validation"), validation))
    return tuple(rows)


def mounted_file_property_rows(path: str | Path) -> tuple[tuple[str, str], ...]:
    """Read Amiga metadata exposed by an active AmigaFS FUSE mount."""

    target = os.fspath(path)

    def value(name: str) -> str | None:
        try:
            return os.getxattr(target, name).decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    source = value("user.amiga.source")
    if source is None:
        return ()
    rows = [(_("Source filesystem"), source)]
    labels = (
        ("user.amiga.path", _("Amiga path")),
        ("user.amiga.volume", _("Volume name")),
        ("user.amiga.protection", _("Protection bits")),
        ("user.amiga.comment", _("Comment")),
        ("user.amiga.link", _("Link")),
    )
    for attribute, label in labels:
        item = value(attribute)
        if item is not None:
            rows.append((label, item))
    return tuple(rows)
