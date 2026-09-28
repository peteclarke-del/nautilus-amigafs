"""Read-only integrity reporting for every supported Amiga medium."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from amigafs.errors import AmigaFSError
from amigafs.i18n import _, ngettext
from amigafs.operations import CancellationCheck, OperationBudget

if TYPE_CHECKING:
    from amigafs.core.image import AmigaImage

VALIDATION_REPORT_SCHEMA_VERSION = 1
COMPATIBILITY_PROFILE_ID = "amigafs-volume"
COMPATIBILITY_PROFILE_VERSION = 1

_BITMAP = re.compile(
    r"bitmap is marked invalid|but the bitmap marks it free|blocks in use but the bitmap has",
    re.IGNORECASE,
)
_DIRECTORY_CACHE = re.compile(
    r"^(?:the (?:directory )?cache of|no directory cache was found|cache block \d+ of)",
    re.IGNORECASE,
)


class FindingSeverity(StrEnum):
    FATAL = "fatal"
    WARNING = "warning"
    ADVICE = "advice"


@dataclass(frozen=True, slots=True)
class IntegrityFinding:
    severity: FindingSeverity
    code: str
    message: str
    path: str | None = None
    volume: int | None = None

    @property
    def severity_label(self) -> str:
        """Return the translated human label without changing the stable enum value."""

        return {
            FindingSeverity.FATAL: _("FATAL"),
            FindingSeverity.WARNING: _("WARNING"),
            FindingSeverity.ADVICE: _("ADVICE"),
        }[self.severity]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VolumeReport:
    """Capacity and identity of one volume at the time it was validated."""

    index: int
    name: str
    title: str
    filesystem: str
    format: str
    total_bytes: int
    free_bytes: int | None
    entries: int
    mounted: bool

    @property
    def used_bytes(self) -> int | None:
        return None if self.free_bytes is None else max(0, self.total_bytes - self.free_bytes)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "used_bytes": self.used_bytes}


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    image_path: str
    image_bytes: int
    image_kind: str
    layout: str
    volumes: tuple[VolumeReport, ...]
    findings: tuple[IntegrityFinding, ...]
    compatibility_profile_id: str = COMPATIBILITY_PROFILE_ID
    compatibility_profile_version: int = COMPATIBILITY_PROFILE_VERSION

    @property
    def fatal_findings(self) -> tuple[IntegrityFinding, ...]:
        return tuple(item for item in self.findings if item.severity is FindingSeverity.FATAL)

    @property
    def warning_findings(self) -> tuple[IntegrityFinding, ...]:
        return tuple(item for item in self.findings if item.severity is FindingSeverity.WARNING)

    @property
    def advice_findings(self) -> tuple[IntegrityFinding, ...]:
        return tuple(item for item in self.findings if item.severity is FindingSeverity.ADVICE)

    @property
    def safe_for_write(self) -> bool:
        return not self.fatal_findings

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": VALIDATION_REPORT_SCHEMA_VERSION,
            "compatibility_profile": {
                "id": self.compatibility_profile_id,
                "version": self.compatibility_profile_version,
            },
            "image": self.image_path,
            "image_bytes": self.image_bytes,
            "image_kind": self.image_kind,
            "layout": self.layout,
            "volumes": [volume.as_dict() for volume in self.volumes],
            "safe_for_write": self.safe_for_write,
            "summary": {
                "fatal": len(self.fatal_findings),
                "warning": len(self.warning_findings),
                "advice": len(self.advice_findings),
            },
            "findings": [item.as_dict() for item in self.findings],
        }

    def format_text(self) -> str:
        """Return the same complete, readable report for CLI and desktop UIs."""

        if not self.findings:
            return _("Filesystem validation passed with no problems.")
        finding_label = ngettext("finding", "findings", len(self.findings))
        lines = [
            _(
                "Validation found {fatal} fatal, {warning} warning, and "
                "{advice} advice {finding_label}:"
            ).format(
                fatal=len(self.fatal_findings),
                warning=len(self.warning_findings),
                advice=len(self.advice_findings),
                finding_label=finding_label,
            )
        ]
        for finding in self.findings:
            location = f" {finding.path}" if finding.path else ""
            lines.append(
                _("- [{severity}] {code}{location}: {message}").format(
                    severity=finding.severity_label,
                    code=finding.code,
                    location=location,
                    message=finding.message,
                )
            )
        return "\n".join(lines)


def classify_problem(problem: str, filesystem: str = "ffs") -> str:
    """Give one engine finding the stable code repair planning keys on."""

    if filesystem not in {"ofs", "ffs"}:
        return "volume.structure"
    if _BITMAP.search(problem):
        return "bitmap.inconsistent"
    if _DIRECTORY_CACHE.search(problem):
        return "dircache.stale"
    return "volume.structure"


def report_for_image(
    image: AmigaImage,
    *,
    cancelled: CancellationCheck | None = None,
    budget: OperationBudget | None = None,
) -> IntegrityReport:
    """Validate every volume of an already open image."""

    operation = budget or OperationBudget.create()
    operation.checkpoint(cancelled)
    findings: list[IntegrityFinding] = []
    volumes: list[VolumeReport] = []
    entries: dict[int, int] = {}
    depths: dict[int, int] = {}
    deepest = 0
    for inode in sorted(image.nodes):
        node = image.nodes[inode]
        entries[node.volume] = entries.get(node.volume, 0) + (0 if node.is_volume_root else 1)
        # A parent is always indexed before its children, so it is already known.
        depth = (
            0 if node.is_volume_root or node.volume < 0 else depths.get(node.parent_inode, 0) + 1
        )
        depths[inode] = depth
        deepest = max(deepest, depth if node.is_dir else depth - 1)
    operation.checkpoint(cancelled, items=len(image.nodes), depth=deepest)
    problems = image.volume_problems()
    operation.checkpoint(cancelled)
    for index in sorted(image.volumes):
        volume = image.volumes[index]
        name = volume.directory or volume.device_name
        mounted = index in problems
        title = image.volume_title(index) if mounted else ""
        total = volume.length
        free: int | None = None
        if mounted:
            mount = image.mount_for(index)
            try:
                total = int(mount.size_bytes())
                free = int(mount.free_bytes())
            except Exception:
                free = None
        volumes.append(
            VolumeReport(
                index=index,
                name=name,
                title=title,
                filesystem=volume.filesystem,
                format=volume.format,
                total_bytes=total,
                free_bytes=free,
                entries=entries.get(index, 0),
                mounted=mounted,
            )
        )
        label = name or title or None
        for problem in problems.get(index, ()):
            operation.checkpoint(cancelled, items=1)
            findings.append(
                IntegrityFinding(
                    severity=FindingSeverity.FATAL,
                    code=classify_problem(problem, volume.filesystem),
                    message=problem,
                    path=label,
                    volume=index,
                )
            )
    for volume, reason in image.unmounted:
        findings.append(
            IntegrityFinding(
                severity=FindingSeverity.ADVICE
                if volume.filesystem == "unknown"
                else FindingSeverity.WARNING,
                code="volume.not_mounted",
                message=_("Partition {name} ({format}) was not mounted: {reason}").format(
                    name=volume.device_name or volume.directory,
                    format=volume.format,
                    reason=reason,
                ),
                path=volume.directory or None,
                volume=volume.index,
            )
        )
    try:
        image_bytes = image.store.size
    except Exception:
        image_bytes = 0
    return IntegrityReport(
        image_path=str(image.source.primary_path),
        image_bytes=image_bytes,
        image_kind=image.source.kind,
        layout=image.layout,
        volumes=tuple(volumes),
        findings=tuple(findings),
        compatibility_profile_id=f"amigafs-{image.source.kind}",
    )


def validate_image_report(
    selected: str | Path,
    *,
    cancelled: CancellationCheck | None = None,
    budget: OperationBudget | None = None,
) -> IntegrityReport:
    """Open and validate one source read-only, returning classified findings."""

    from amigafs.core.formats import resolve_image
    from amigafs.core.image import AmigaImage
    from amigafs.errors import OperationCancelled, OperationLimitExceeded

    operation = budget or OperationBudget.create()
    operation.checkpoint(cancelled)
    source = resolve_image(selected)
    try:
        image = AmigaImage.open(source, writable=False)
    except (OperationCancelled, OperationLimitExceeded):
        raise
    except (AmigaFSError, MemoryError, RecursionError) as exc:
        try:
            size = 0 if source.container == "greaseweazle" else source.primary_path.stat().st_size
        except OSError:
            size = 0
        return IntegrityReport(
            image_path=str(source.primary_path),
            image_bytes=size,
            image_kind=source.kind,
            layout="",
            volumes=(),
            findings=(
                IntegrityFinding(
                    severity=FindingSeverity.FATAL,
                    code="image.open_failed",
                    message=_("The image could not be opened for validation: {error}").format(
                        error=exc
                    ),
                ),
            ),
            compatibility_profile_id=f"amigafs-{source.kind}",
        )
    try:
        return report_for_image(image, cancelled=cancelled, budget=operation)
    finally:
        image.close(clean=False)


def require_safe_for_write(report: IntegrityReport) -> None:
    """Reject a writable mount with a concise summary of fatal integrity findings."""

    if report.safe_for_write:
        return
    first = report.fatal_findings[0]
    count = len(report.fatal_findings)
    raise AmigaFSError(
        ngettext(
            "Writable mount refused: validation found {count} fatal problem. {code}: {message}",
            "Writable mount refused: validation found {count} fatal problems. {code}: {message}",
            count,
        ).format(count=count, code=first.code, message=first.message)
    )


__all__ = [
    "COMPATIBILITY_PROFILE_ID",
    "COMPATIBILITY_PROFILE_VERSION",
    "VALIDATION_REPORT_SCHEMA_VERSION",
    "FindingSeverity",
    "IntegrityFinding",
    "IntegrityReport",
    "VolumeReport",
    "classify_problem",
    "report_for_image",
    "require_safe_for_write",
    "validate_image_report",
]
