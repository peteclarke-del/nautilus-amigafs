import json
import os
from pathlib import Path
from unittest.mock import patch

from amigafs.mounts import MountRecord, runtime_root
from amigafs.retention import cleanup_retained_state

DAY = 24 * 60 * 60


def _age(path: Path, timestamp: float) -> None:
    os.utime(path, (timestamp, timestamp), follow_symlinks=False)


def test_cleanup_removes_only_inactive_aged_runtime_logs(
    tmp_path: Path, monkeypatch: object
) -> None:
    now = 100 * DAY
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))  # type: ignore[attr-defined]
    (tmp_path / "runtime").mkdir()
    root = runtime_root()
    root.mkdir(parents=True)
    old = root / "old.log"
    active = root / "active.log"
    recent = root / "recent.log"
    for path in (old, active, recent):
        path.write_text("log", encoding="utf-8")
    _age(old, now - 8 * DAY)
    _age(active, now - 8 * DAY)
    _age(recent, now - DAY)

    with patch(
        "amigafs.retention.active_mounts",
        return_value=[MountRecord("/mounts/active", "disc.adf", "ro")],
    ):
        result = cleanup_retained_state(now=now)

    assert result.runtime_logs == 1
    assert not old.exists()
    assert active.exists()
    assert recent.exists()


def test_cleanup_preserves_failed_or_recovery_related_audits(tmp_path: Path) -> None:
    now = 200 * DAY
    root = tmp_path / "state" / "amigafs" / "repair-audits"
    root.mkdir(parents=True)
    completed = root / "completed.json"
    failed = root / "failed.json"
    retained = root / "retained.json"
    for path, payload in (
        (completed, {"status": "completed", "checkpoint_retained": False}),
        (failed, {"status": "failed", "checkpoint_retained": False}),
        (retained, {"status": "completed", "checkpoint_retained": True}),
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")
        _age(path, now - 91 * DAY)

    result = cleanup_retained_state(now=now)

    assert result.repair_audits == 1
    assert not completed.exists()
    assert failed.exists()
    assert retained.exists()


def test_cleanup_removes_only_known_old_checkpoint_orphans(tmp_path: Path) -> None:
    now = 300 * DAY
    root = tmp_path / "state" / "amigafs" / "recovery"
    orphan = root / ("a" * 64)
    pending = root / ("b" * 64)
    unknown = root / ("c" * 64)
    for directory in (orphan, pending, unknown):
        directory.mkdir(parents=True)
    (orphan / "undo.journal").write_bytes(b"journal")
    (orphan / "workspace.img").write_bytes(b"working copy")
    (orphan / "source.adf").write_bytes(b"floppy as read")
    (pending / "manifest.json").write_text("{}", encoding="utf-8")
    (unknown / "keep.txt").write_text("user data", encoding="utf-8")
    for directory in (orphan, pending, unknown):
        for entry in directory.iterdir():
            _age(entry, now - 8 * DAY)
        _age(directory, now - 8 * DAY)

    result = cleanup_retained_state(now=now)

    assert result.orphan_checkpoints == 1
    assert not orphan.exists()
    assert pending.exists()
    assert unknown.exists()


def test_cleanup_failure_in_one_state_area_does_not_block_the_others() -> None:
    with (
        patch("amigafs.retention._cleanup_runtime_logs", side_effect=OSError("unreadable")),
        patch("amigafs.retention._cleanup_completed_audits", return_value=2),
        patch("amigafs.retention._cleanup_orphan_checkpoints", return_value=3),
    ):
        result = cleanup_retained_state(now=0)

    assert result.runtime_logs == 0
    assert result.repair_audits == 2
    assert result.orphan_checkpoints == 3
