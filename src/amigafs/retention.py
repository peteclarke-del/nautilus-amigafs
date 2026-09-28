"""Conservative cleanup for disposable AmigaFS runtime and state files."""

from __future__ import annotations

import json
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from amigafs.core.repair import audit_root
from amigafs.errors import AmigaFSError
from amigafs.mounts import active_mounts, runtime_root
from amigafs.recovery import state_root

RUNTIME_LOG_RETENTION_DAYS = 7
COMPLETED_AUDIT_RETENTION_DAYS = 90
ORPHAN_CHECKPOINT_RETENTION_DAYS = 7
_IDENTITY = re.compile(r"[0-9a-f]{64}")
_ORPHAN_CHECKPOINT_FILES = frozenset(
    {"undo.journal", "workspace.img", "write-back.adf", "manifest.tmp"}
)
_ORPHAN_CHECKPOINT_PREFIXES = ("source.", "candidate-")


def _disposable(name: str) -> bool:
    return name in _ORPHAN_CHECKPOINT_FILES or name.startswith(_ORPHAN_CHECKPOINT_PREFIXES)


@dataclass(frozen=True, slots=True)
class CleanupResult:
    runtime_logs: int = 0
    repair_audits: int = 0
    orphan_checkpoints: int = 0


def _older_than(path: Path, cutoff: float) -> bool:
    try:
        return path.stat(follow_symlinks=False).st_mtime < cutoff
    except OSError:
        return False


def _cleanup_runtime_logs(cutoff: float) -> int:
    root = runtime_root()
    if not root.is_dir():
        return 0
    try:
        active_names = {Path(record.mountpoint).name for record in active_mounts()}
    except (AmigaFSError, OSError):
        return 0
    removed = 0
    for path in root.glob("*.log"):
        try:
            metadata = path.stat(follow_symlinks=False)
            if (
                stat.S_ISREG(metadata.st_mode)
                and path.stem not in active_names
                and metadata.st_mtime < cutoff
            ):
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _cleanup_completed_audits(cutoff: float) -> int:
    root = audit_root()
    if not root.is_dir():
        return 0
    removed = 0
    for path in root.glob("*.json"):
        try:
            metadata = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mtime >= cutoff:
                continue
            if metadata.st_size > 1024 * 1024:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(payload, dict)
                and payload.get("status") == "completed"
                and payload.get("checkpoint_retained") is False
            ):
                path.unlink()
                removed += 1
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return removed


def _cleanup_orphan_checkpoints(cutoff: float) -> int:
    root = state_root()
    if not root.is_dir():
        return 0
    removed = 0
    for directory in root.iterdir():
        try:
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or _IDENTITY.fullmatch(directory.name) is None
                or (directory / "manifest.json").exists()
            ):
                continue
            entries = list(directory.iterdir())
            if any(
                not _disposable(entry.name)
                or entry.is_symlink()
                or not stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode)
                for entry in entries
            ):
                continue
            if not _older_than(directory, cutoff) or any(
                not _older_than(entry, cutoff) for entry in entries
            ):
                continue
            for entry in entries:
                entry.unlink()
            directory.rmdir()
            removed += 1
        except OSError:
            continue
    return removed


def _best_effort(cleanup: Callable[[float], int], cutoff: float) -> int:
    try:
        return cleanup(cutoff)
    except (AmigaFSError, OSError):
        return 0


def cleanup_retained_state(*, now: float | None = None) -> CleanupResult:
    """Remove only aged disposable state; recovery evidence is always retained."""

    timestamp = time.time() if now is None else now
    day = 24 * 60 * 60
    return CleanupResult(
        runtime_logs=_best_effort(
            _cleanup_runtime_logs, timestamp - RUNTIME_LOG_RETENTION_DAYS * day
        ),
        repair_audits=_best_effort(
            _cleanup_completed_audits, timestamp - COMPLETED_AUDIT_RETENTION_DAYS * day
        ),
        orphan_checkpoints=_best_effort(
            _cleanup_orphan_checkpoints, timestamp - ORPHAN_CHECKPOINT_RETENTION_DAYS * day
        ),
    )


__all__ = [
    "COMPLETED_AUDIT_RETENTION_DAYS",
    "CleanupResult",
    "ORPHAN_CHECKPOINT_RETENTION_DAYS",
    "RUNTIME_LOG_RETENTION_DAYS",
    "cleanup_retained_state",
]
