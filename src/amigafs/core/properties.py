"""Typed compatibility, capacity and validation metadata for Amiga media."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from amigafs.core.containers import floppy_format_for_size
from amigafs.core.formats import ResolvedImage, resolve_image
from amigafs.core.image import AmigaImage
from amigafs.core.validation import report_for_image
from amigafs.i18n import N_, _
from amigafs.operations import OperationBudget

_KIND_LABELS = {
    "floppy-image": N_("Amiga floppy image (ADF)"),
    "hard-disc-image": N_("Amiga hard-disc image"),
    "physical-disc": N_("Physical Amiga disc"),
    "physical-floppy": N_("Physical Amiga floppy"),
    "compressed-image": N_("Compressed Amiga image"),
    "dms-archive": N_("DiskMasher archive"),
    "extended-adf": N_("Extended ADF"),
    "flux-image": N_("Track-level floppy image"),
    "kickstart-rom": N_("Kickstart ROM"),
}

_CONTAINER_LABELS = {
    "gzip": "gzip (ADZ/HDZ)",
    "dms": "DiskMasher (DMS)",
    "extended-adf": "UAE extended ADF",
    "hfe": "HxC HFE",
    "scp": "SuperCard Pro",
    "ipf": "SPS IPF",
    "greaseweazle": "Greaseweazle",
}

_LAYOUT_LABELS = {
    "single": N_("One volume, no partition table"),
    "rdb": N_("Rigid Disk Block partition table"),
    "kickstart": N_("Resident module list"),
}


@dataclass(frozen=True, slots=True)
class VolumeProperties:
    """One partition or volume as a user interface presents it."""

    index: int
    name: str
    title: str
    filesystem: str
    format: str
    dos_type: str
    bootable: bool
    boot_priority: int
    capacity_bytes: int
    used_bytes: int | None
    free_bytes: int | None
    files: int
    directories: int
    block_size: int
    first_cylinder: int | None
    last_cylinder: int | None
    mounted: bool
    writable: bool
    problem: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ImageProperties:
    """Stable, serialisable metadata shown by user interfaces."""

    image_path: str
    image_name: str
    image_kind: str
    image_type: str
    container: str
    layout: str
    layout_label: str
    capacity_bytes: int
    cylinders: int | None
    heads: int | None
    sectors_per_track: int | None
    sector_size: int
    disc_vendor: str
    disc_product: str
    disc_revision: str
    read_write_supported: bool
    volumes: tuple[VolumeProperties, ...]
    validation_state: str
    fatal_findings: int
    warning_findings: int
    advice_findings: int
    first_finding: str

    def as_dict(self) -> dict[str, object]:
        result: dict[str, Any] = asdict(self)
        result["volumes"] = [volume.as_dict() for volume in self.volumes]
        return result


def kind_label(kind: str) -> str:
    return _(_KIND_LABELS.get(kind, kind))


def dos_type_text(dos_type: bytes) -> str:
    """Render a DOS type the way Amiga tools print it, for example ``DOS\\3``."""

    if len(dos_type) != 4:
        return ""
    head = dos_type[:3].decode("latin-1", "replace")
    tail = dos_type[3]
    return f"{head}\\{tail}" if tail < 32 else f"{head}{chr(tail)}"


def properties_for_image(
    image: AmigaImage, *, budget: OperationBudget | None = None
) -> ImageProperties:
    """Describe an already open image."""

    operation = budget or OperationBudget.create()
    report = report_for_image(image, budget=operation)
    reports = {volume.index: volume for volume in report.volumes}
    files: dict[int, int] = {}
    directories: dict[int, int] = {}
    for node in image.nodes.values():
        if node.volume < 0 or node.is_volume_root:
            continue
        counter = directories if node.is_dir else files
        counter[node.volume] = counter.get(node.volume, 0) + 1
    problems = {volume.index: reason for volume, reason in image.unmounted}
    volumes = []
    for index in sorted(image.volumes):
        volume = image.volumes[index]
        summary = reports.get(index)
        geometry = volume.geometry if volume.partition is not None else None
        volumes.append(
            VolumeProperties(
                index=index,
                name=volume.device_name or volume.directory,
                title=summary.title if summary is not None else "",
                filesystem=volume.filesystem,
                format=volume.format,
                dos_type=dos_type_text(volume.dos_type),
                bootable=volume.bootable,
                boot_priority=volume.boot_priority,
                capacity_bytes=summary.total_bytes if summary is not None else volume.length,
                used_bytes=summary.used_bytes if summary is not None else None,
                free_bytes=summary.free_bytes if summary is not None else None,
                files=files.get(index, 0),
                directories=directories.get(index, 0),
                block_size=volume.block_size,
                first_cylinder=geometry.low_cylinder if geometry is not None else None,
                last_cylinder=geometry.high_cylinder if geometry is not None else None,
                mounted=index not in problems,
                writable=image.source.capabilities.mount_read_write
                and volume.filesystem not in {"unknown", "kickfs"}
                and index not in problems,
                problem=problems.get(index, ""),
            )
        )
    disk = image.media.rigid_disk
    cylinders: int | None = None
    heads: int | None = None
    sectors: int | None = None
    if disk is not None:
        cylinders, heads, sectors = disk.cylinders, disk.heads, disk.sectors
    else:
        floppy = floppy_format_for_size(image.store.size)
        if floppy is not None and image.layout == "single":
            cylinders, heads, sectors = floppy.cylinders, 2, floppy.sectors_per_track
    findings = report.findings
    state = "problems" if report.fatal_findings or report.warning_findings else "passed"
    first = next(
        (
            f"{finding.code}: {finding.message}"
            for finding in (*report.fatal_findings, *report.warning_findings)
        ),
        "",
    )
    return ImageProperties(
        image_path=str(image.source.primary_path),
        image_name=image.source.name,
        image_kind=image.source.kind,
        image_type=kind_label(image.source.kind),
        container=_CONTAINER_LABELS.get(image.source.container or "", ""),
        layout=image.layout,
        layout_label=_(_LAYOUT_LABELS.get(image.layout, image.layout)),
        capacity_bytes=image.store.size,
        cylinders=cylinders,
        heads=heads,
        sectors_per_track=sectors,
        sector_size=disk.block_size if disk is not None else 512,
        disc_vendor=disk.disk_vendor if disk is not None else "",
        disc_product=disk.disk_product if disk is not None else "",
        disc_revision=disk.disk_revision if disk is not None else "",
        read_write_supported=image.source.capabilities.mount_read_write,
        volumes=tuple(volumes),
        validation_state=state,
        fatal_findings=len(report.fatal_findings),
        warning_findings=len(report.warning_findings),
        advice_findings=len(findings) - len(report.fatal_findings) - len(report.warning_findings),
        first_finding=first,
    )


def read_image_properties(
    selected: str | Path | ResolvedImage, *, budget: OperationBudget | None = None
) -> ImageProperties:
    """Open one source read-only and describe it."""

    source = selected if isinstance(selected, ResolvedImage) else resolve_image(selected)
    image = AmigaImage.open(source, writable=False)
    try:
        return properties_for_image(image, budget=budget)
    finally:
        image.close(clean=False)


__all__ = [
    "ImageProperties",
    "VolumeProperties",
    "dos_type_text",
    "kind_label",
    "properties_for_image",
    "read_image_properties",
]
