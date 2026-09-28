"""Repair planning and tightly controlled low-risk repair application."""

from __future__ import annotations

import json
import os
import pwd
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from amigafs.core.formats import resolve_image
from amigafs.core.image import AmigaImage
from amigafs.core.validation import IntegrityFinding, IntegrityReport, validate_image_report
from amigafs.errors import AmigaFSError
from amigafs.i18n import N_, _
from amigafs.operations import OperationBudget, ProgressCallback, report_progress
from amigafs.safe_paths import atomic_write_private_text

SUPPORTED_REPAIR_ACTIONS = frozenset({"rebuild_allocation_bitmap", "rebuild_directory_cache"})
REPAIRABLE_FINDING_CODES = frozenset({"bitmap.inconsistent", "dircache.stale"})


class RepairRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class RepairAction:
    action: str
    title: str
    description: str
    risk: RepairRisk
    automatic_candidate: bool
    requires_manual_decision: bool
    finding_codes: tuple[str, ...]
    paths: tuple[str, ...]
    volumes: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RepairPlan:
    report: IntegrityReport
    actions: tuple[RepairAction, ...]

    @property
    def clean(self) -> bool:
        return not self.report.findings

    @property
    def application_supported(self) -> bool:
        planned = {code for action in self.actions for code in action.finding_codes}
        return (
            bool(self.actions)
            and all(
                finding.code in planned and finding.code in REPAIRABLE_FINDING_CODES
                for finding in (*self.report.fatal_findings, *self.report.warning_findings)
            )
            and all(
                action.action in SUPPORTED_REPAIR_ACTIONS
                and action.risk is RepairRisk.LOW
                and action.automatic_candidate
                and not action.requires_manual_decision
                for action in self.actions
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": "dry-run",
            "application_supported": self.application_supported,
            "clean": self.clean,
            "validation": self.report.as_dict(),
            "actions": [action.as_dict() for action in self.actions],
        }

    def format_text(self) -> str:
        if self.clean:
            return _("No repair actions are needed; validation found no problems.")
        finding_count = len(self.report.findings)
        action_count = len(self.actions)
        lines = [
            _("Dry-run repair plan (the image was not modified):"),
            _("Validation findings: {findings}; planned actions: {actions}.").format(
                findings=finding_count, actions=action_count
            ),
        ]
        if not self.actions:
            lines.append(_("No repair action is proposed for informational findings only."))
        for index, action in enumerate(self.actions, 1):
            mode = _("candidate") if action.automatic_candidate else _("manual decision required")
            risk = {
                RepairRisk.LOW: _("low"),
                RepairRisk.MEDIUM: _("medium"),
                RepairRisk.HIGH: _("high"),
            }[action.risk]
            lines.append(
                _("{index}. {title} [{risk} risk; {mode}]").format(
                    index=index, title=action.title, risk=risk, mode=mode
                )
            )
            lines.append(f"   {action.description}")
            lines.append(
                _("   Findings: {findings}").format(findings=", ".join(action.finding_codes))
            )
            if action.paths:
                lines.append(_("   Paths: {paths}").format(paths=", ".join(action.paths)))
        if self.application_supported:
            lines.append(
                _(
                    "This complete plan can be applied with 'amigafs repair IMAGE "
                    "--confirm IMAGE_FILENAME'."
                )
            )
        else:
            lines.append(_("This plan cannot be applied automatically; no changes are permitted."))
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class RepairResult:
    audit_path: str
    actions: tuple[RepairAction, ...]
    report: IntegrityReport

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "completed",
            "audit": self.audit_path,
            "actions": [action.as_dict() for action in self.actions],
            "validation": self.report.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class _ActionTemplate:
    action: str
    title: str
    description: str
    risk: RepairRisk
    automatic_candidate: bool
    requires_manual_decision: bool


_BITMAP = _ActionTemplate(
    "rebuild_allocation_bitmap",
    N_("Rebuild the block-allocation bitmap"),
    N_(
        "Mark every block the directory tree reaches as in use and everything else as free, "
        "as the Amiga's own disk validator does. No file or directory is changed."
    ),
    RepairRisk.LOW,
    True,
    False,
)
_DIRECTORY_CACHE = _ActionTemplate(
    "rebuild_directory_cache",
    N_("Rebuild the directory caches"),
    N_(
        "Rewrite each directory's cache from the file headers it lists. No file or "
        "directory is changed."
    ),
    RepairRisk.LOW,
    True,
    False,
)
_UNREADABLE = _ActionTemplate(
    "restore_unreadable_structure",
    N_("Restore damaged filesystem structures"),
    N_(
        "The volume cannot be parsed deeply enough for safe automatic reconstruction; "
        "use a known-good copy, or repair it on an Amiga or in an emulator."
    ),
    RepairRisk.HIGH,
    False,
    True,
)


def _template_for(finding: IntegrityFinding) -> _ActionTemplate | None:
    if finding.code == "bitmap.inconsistent":
        return _BITMAP
    if finding.code == "dircache.stale":
        return _DIRECTORY_CACHE
    if finding.code in {"volume.structure", "image.open_failed"}:
        return _UNREADABLE
    return None


def plan_repairs_from_report(report: IntegrityReport) -> RepairPlan:
    """Group an existing validation report into deterministic dry-run actions."""

    grouped: dict[str, tuple[_ActionTemplate, set[str], set[str], set[int]]] = {}
    for finding in report.findings:
        template = _template_for(finding)
        if template is None:
            continue
        _template, codes, paths, volumes = grouped.setdefault(
            template.action, (template, set(), set(), set())
        )
        codes.add(finding.code)
        if finding.path:
            paths.add(finding.path)
        if finding.volume is not None:
            volumes.add(finding.volume)
    actions = tuple(
        RepairAction(
            action=template.action,
            title=_(template.title),
            description=_(template.description),
            risk=template.risk,
            automatic_candidate=template.automatic_candidate,
            requires_manual_decision=template.requires_manual_decision,
            finding_codes=tuple(sorted(codes)),
            paths=tuple(sorted(paths)),
            volumes=tuple(sorted(volumes)),
        )
        for template, codes, paths, volumes in sorted(
            grouped.values(), key=lambda item: item[0].action
        )
    )
    return RepairPlan(report=report, actions=actions)


def plan_repairs(selected: str | Path, *, budget: OperationBudget | None = None) -> RepairPlan:
    """Validate an image and group its findings into deterministic dry-run actions."""

    return plan_repairs_from_report(validate_image_report(selected, budget=budget))


def audit_root() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured).expanduser() / "amigafs" / "repair-audits"
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / ".local" / "state" / "amigafs" / "repair-audits"


def _write_audit(path: Path, payload: dict[str, Any]) -> None:
    root = audit_root()
    content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    atomic_write_private_text(path, content, anchor=root.parent.parent)


def _apply_action(image: AmigaImage, action: RepairAction) -> None:
    from amigafs.core.bitmap import rebuild_bitmap

    for volume in action.volumes:
        if image.volumes[volume].filesystem not in {"ofs", "ffs"}:
            raise AmigaFSError(_("This repair applies only to OFS and FFS volumes."))
        if action.action == "rebuild_allocation_bitmap":
            image.run_volume_repair(volume, action.action, rebuild_bitmap)
        elif action.action == "rebuild_directory_cache":
            image.run_volume_repair(
                volume, action.action, lambda mount: mount.volume.rebuild_dircache()
            )
        else:
            raise AmigaFSError(
                _("unsupported repair action: {action}").format(action=action.action)
            )


def _ordered(actions: tuple[RepairAction, ...]) -> tuple[RepairAction, ...]:
    # Rebuilding a directory cache may allocate blocks, so the bitmap has to be
    # trustworthy first.
    order = {"rebuild_allocation_bitmap": 0, "rebuild_directory_cache": 1}
    return tuple(sorted(actions, key=lambda action: order.get(action.action, 9)))


def apply_repairs(
    selected: str | Path,
    *,
    confirmation: str,
    progress: ProgressCallback | None = None,
    budget: OperationBudget | None = None,
) -> RepairResult:
    """Apply a complete low-risk plan with confirmation, checkpointing and audit."""

    operation = budget or OperationBudget.create()
    operation.checkpoint()
    report_progress(progress, 0, _("Planning repair…"))
    source = resolve_image(selected)
    if not source.capabilities.repair:
        raise AmigaFSError(_("This image format has no supported repair operation."))
    expected = source.primary_path.name
    if confirmation != expected:
        raise AmigaFSError(
            _("Repair confirmation must exactly match the image filename: {name}").format(
                name=expected
            )
        )
    plan = plan_repairs(source.primary_path, budget=operation)
    report_progress(progress, 10, _("Repair plan validated"))
    if plan.clean:
        raise AmigaFSError(_("Validation found no problems, so there is nothing to repair."))
    if not plan.application_supported:
        raise AmigaFSError(
            _(
                "The complete repair plan is not eligible for automatic application; "
                "no checkpoint or image change was made."
            )
        )

    audit_path = (
        audit_root() / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4()}.json"
    )
    payload: dict[str, Any] = {
        "audit_version": 1,
        "audit_id": audit_path.stem,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "planned",
        "image": str(source.primary_path),
        "confirmation": confirmation,
        "checkpoint_created": False,
        "checkpoint_retained": False,
        "plan": plan.as_dict(),
        "applied_actions": [],
        "post_validation": None,
    }
    try:
        _write_audit(audit_path, payload)
        report_progress(progress, 15, _("Repair audit created"))
    except (OSError, MemoryError) as exc:
        raise AmigaFSError(
            _("Could not create the mandatory repair audit: {error}").format(error=exc)
        ) from exc

    image: AmigaImage | None = None
    checkpoint_completed = False
    try:
        report_progress(progress, 20, _("Revalidating image before checkpoint creation…"))
        image = AmigaImage.open(
            source,
            writable=True,
            repairable_codes=REPAIRABLE_FINDING_CODES,
            operation_budget=operation,
        )
        report_progress(progress, 60, _("Recovery checkpoint ready"))
        payload["status"] = "applying"
        payload["checkpoint_created"] = True
        _write_audit(audit_path, payload)
        actions = _ordered(plan.actions)
        for index, action in enumerate(actions, 1):
            operation.checkpoint(items=1)
            action_percent = 60 + int((index - 1) * 15 / len(actions))
            report_progress(
                progress,
                action_percent,
                _("Applying: {action}").format(action=action.title),
            )
            _apply_action(image, action)
            payload["applied_actions"].append(action.as_dict())
            _write_audit(audit_path, payload)

        report_progress(progress, 78, _("Verifying the complete repaired image…"))
        report = image.integrity_report(budget=operation)
        if report.fatal_findings or report.warning_findings:
            first = (*report.fatal_findings, *report.warning_findings)[0]
            raise AmigaFSError(
                _("Post-repair validation failed: {detail}.").format(
                    detail=f"{first.code}: {first.message}"
                )
            )
        payload["status"] = "verified"
        payload["post_validation"] = report.as_dict()
        _write_audit(audit_path, payload)
        report_progress(progress, 92, _("Repair verified; finalising checkpoint…"))
        image.close()
        checkpoint_completed = True
        image = None
        payload["status"] = "completed"
        payload["completed_at"] = datetime.now(UTC).isoformat()
        _write_audit(audit_path, payload)
        report_progress(progress, 100, _("Repair completed and verified"))
        return RepairResult(str(audit_path), plan.actions, report)
    except Exception as exc:
        if image is not None:
            with suppress(Exception):
                image.close(clean=False)
        payload["status"] = "failed"
        payload["failed_at"] = datetime.now(UTC).isoformat()
        payload["error"] = str(exc)
        payload["checkpoint_retained"] = bool(
            payload["checkpoint_created"] and not checkpoint_completed
        )
        with suppress(OSError, MemoryError):
            _write_audit(audit_path, payload)
        if isinstance(exc, AmigaFSError):
            raise AmigaFSError(
                _("{error} Audit: {audit}").format(error=exc, audit=audit_path)
            ) from exc
        if checkpoint_completed:
            raise AmigaFSError(
                _(
                    "Repair completed and verified, but its audit could not be marked complete. "
                    "The verified audit was retained at {audit}: {error}"
                ).format(audit=audit_path, error=exc)
            ) from exc
        raise AmigaFSError(
            _("Repair failed; the checkpoint was retained. Audit: {audit}: {error}").format(
                audit=audit_path, error=exc
            )
        ) from exc


__all__ = [
    "REPAIRABLE_FINDING_CODES",
    "SUPPORTED_REPAIR_ACTIONS",
    "RepairAction",
    "RepairPlan",
    "RepairResult",
    "RepairRisk",
    "apply_repairs",
    "audit_root",
    "plan_repairs",
    "plan_repairs_from_report",
]
