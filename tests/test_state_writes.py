import errno
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from amigafs.core.image import ROOT_INODE, AmigaImage
from amigafs.core.repair import _write_audit
from amigafs.errors import AmigaFSError
from amigafs.mounts import register_mount, runtime_root
from amigafs.preferences import mount_location, preferences_path, set_mount_location
from amigafs.recovery import pending_recovery, recover_image
from amigafs.safe_paths import atomic_write_private_text
from tests.image_fixture import create_floppy


def test_private_atomic_write_retries_interruption_and_short_writes(tmp_path: Path) -> None:
    anchor = tmp_path / "state"
    target = anchor / "amigafs" / "record.json"
    real_write = os.write
    calls = 0

    def interrupted_short_write(descriptor: int, content: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InterruptedError(errno.EINTR, "interrupted")
        return real_write(descriptor, content[:3])

    with patch("amigafs.safe_paths.os.write", side_effect=interrupted_short_write):
        atomic_write_private_text(target, '{"complete": true}\n', anchor=anchor)

    assert json.loads(target.read_text(encoding="utf-8")) == {"complete": True}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert calls > 2
    assert not list(target.parent.glob(".*.tmp"))


@pytest.mark.parametrize("failure_point", ["write", "replace"])
def test_private_atomic_write_failure_preserves_last_good_file(
    tmp_path: Path, failure_point: str
) -> None:
    anchor = tmp_path / "state"
    target = anchor / "amigafs" / "record.json"
    atomic_write_private_text(target, "old state\n", anchor=anchor)
    error = OSError(errno.ENOSPC, "disk full")
    patched = (
        patch("amigafs.safe_paths.os.write", side_effect=error)
        if failure_point == "write"
        else patch("amigafs.safe_paths.os.replace", side_effect=error)
    )

    with patched, pytest.raises(OSError) as raised:
        atomic_write_private_text(target, "new state\n", anchor=anchor)

    assert raised.value.errno == errno.ENOSPC
    assert target.read_text(encoding="utf-8") == "old state\n"
    assert not list(target.parent.glob(".*.tmp"))


def test_disk_full_preference_update_preserves_previous_choice(tmp_path: Path) -> None:
    set_mount_location("sidebar")
    before = preferences_path().read_bytes()

    with (
        patch(
            "amigafs.safe_paths.os.write",
            side_effect=OSError(errno.ENOSPC, "disk full"),
        ),
        pytest.raises(AmigaFSError, match="Could not save"),
    ):
        set_mount_location(str(tmp_path / "new-mounts"))

    assert preferences_path().read_bytes() == before
    assert mount_location().mode == "sidebar"
    assert not list(preferences_path().parent.glob(".*.tmp"))


def test_low_memory_preference_update_preserves_previous_choice() -> None:
    set_mount_location("sidebar")
    before = preferences_path().read_bytes()

    with (
        patch("amigafs.preferences.json.dumps", side_effect=MemoryError("memory exhausted")),
        pytest.raises(AmigaFSError, match="Could not save"),
    ):
        set_mount_location("runtime")

    assert preferences_path().read_bytes() == before
    assert mount_location().mode == "sidebar"


def test_disk_full_mount_registration_leaves_no_partial_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    image_path = create_floppy(tmp_path)
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()

    with (
        patch(
            "amigafs.safe_paths.os.write",
            side_effect=OSError(errno.ENOSPC, "disk full"),
        ),
        pytest.raises(AmigaFSError, match="Could not record"),
    ):
        register_mount(image_path, mountpoint, read_write=False)

    assert not list((runtime_root() / "mounts").glob("*.json"))
    assert not list((runtime_root() / "mounts").glob(".*.tmp"))


def test_disk_full_audit_update_preserves_last_durable_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    audit = state / "amigafs" / "repair-audits" / "audit.json"
    _write_audit(audit, {"status": "planned"})
    before = audit.read_bytes()

    with (
        patch(
            "amigafs.safe_paths.os.write",
            side_effect=OSError(errno.ENOSPC, "disk full"),
        ),
        pytest.raises(OSError) as raised,
    ):
        _write_audit(audit, {"status": "completed"})

    assert raised.value.errno == errno.ENOSPC
    assert audit.read_bytes() == before
    assert not list(audit.parent.glob(".*.tmp"))


def test_low_memory_audit_update_preserves_last_durable_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    audit = state / "amigafs" / "repair-audits" / "audit.json"
    _write_audit(audit, {"status": "planned"})
    before = audit.read_bytes()

    with (
        patch("amigafs.core.repair.json.dumps", side_effect=MemoryError("memory exhausted")),
        pytest.raises(MemoryError, match="memory exhausted"),
    ):
        _write_audit(audit, {"status": "completed"})

    assert audit.read_bytes() == before
    assert not list(audit.parent.glob(".*.tmp"))


@pytest.mark.parametrize(
    "failure",
    [OSError(errno.ENOSPC, "disk full"), MemoryError("memory exhausted")],
)
def test_failed_checkpoint_creation_leaves_no_trace_and_an_untouched_image(
    tmp_path: Path, failure: Exception
) -> None:
    image_path = create_floppy(tmp_path)
    before = image_path.read_bytes()

    with (
        patch("amigafs.recovery._write_manifest", side_effect=failure),
        pytest.raises(AmigaFSError, match="Could not create.*(disk full|memory exhausted)"),
    ):
        AmigaImage.open(image_path, writable=True)

    assert image_path.read_bytes() == before
    assert pending_recovery(image_path) is None
    recovery = tmp_path / "state" / "amigafs" / "recovery"
    if recovery.exists():
        assert not list(recovery.rglob("manifest.json"))
        assert not list(recovery.rglob("undo.journal"))
    # The failure was transient: the image opens normally afterwards.
    with AmigaImage.open(image_path, writable=True) as image:
        image.create_file(ROOT_INODE, b"Fine")


def test_disk_full_while_journalling_never_reaches_the_image(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    before = image_path.read_bytes()
    image = AmigaImage.open(image_path, writable=True)
    try:
        journal = image.store.journal
        assert journal is not None
        with (
            patch.object(journal, "record", side_effect=OSError(errno.ENOSPC, "disk full")),
            pytest.raises(AmigaFSError, match="could not be written to the medium"),
        ):
            image.create_file(ROOT_INODE, b"Never")
        # The before-images were not made durable, so nothing was written.
        assert image_path.read_bytes() == before
        with pytest.raises(AmigaFSError, match="session has failed"):
            image.create_file(ROOT_INODE, b"Refused")
    finally:
        image.close()
    assert pending_recovery(image_path) is not None
    recover_image(image_path, restore=True)
    assert image_path.read_bytes() == before
