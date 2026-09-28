import fcntl
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from amigafs.core.blockio import ImageStore
from amigafs.core.image import AmigaImage
from amigafs.errors import AmigaFSError
from amigafs.recovery import pending_recovery
from tests.image_fixture import create_floppy, create_hard_disc


@pytest.mark.parametrize("kind", ["floppy", "hard-disc"])
def test_writable_open_refuses_hard_linked_image(tmp_path: Path, kind: str) -> None:
    image_path = (
        create_floppy(tmp_path) if kind == "floppy" else create_hard_disc(tmp_path, capacity="4MB")
    )
    os.link(image_path, tmp_path / "linked.bin")

    with pytest.raises(AmigaFSError, match="hard links"):
        AmigaImage.open(image_path, writable=True)
    assert pending_recovery(image_path) is None

    with AmigaImage.open(image_path) as image:
        assert not image.writable


def test_open_rejects_path_replaced_after_lock(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    replacement = tmp_path / "replacement.adf"
    replacement.write_bytes(image_path.read_bytes())
    real_flock = fcntl.flock

    def replace_between_lock_and_verification(handle: object, operation: int) -> None:
        real_flock(handle, operation)  # type: ignore[arg-type]
        os.replace(replacement, image_path)

    with (
        patch(
            "amigafs.core.blockio.fcntl.flock",
            side_effect=replace_between_lock_and_verification,
        ),
        pytest.raises(AmigaFSError, match="changed while AmigaFS was opening"),
    ):
        ImageStore.open(image_path, writable=False)

    ImageStore.open(image_path, writable=False).close()


def test_image_opened_through_a_symbolic_link_is_locked_by_its_real_identity(
    tmp_path: Path,
) -> None:
    image_path = create_floppy(tmp_path)
    link = tmp_path / "alias.adf"
    link.symlink_to(image_path)
    with (
        AmigaImage.open(link, writable=True) as image,
        pytest.raises(AmigaFSError, match="another AmigaFS process"),
    ):
        assert image.source.primary_path == image_path.resolve()
        AmigaImage.open(image_path)


def test_closing_an_image_releases_its_lock(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)

    image = AmigaImage.open(image_path)
    image.close()

    with AmigaImage.open(image_path, writable=True) as writable:
        assert writable.writable


def test_lock_blocks_other_amigafs_processes(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    script = """
import sys
from amigafs.core.image import AmigaImage
from amigafs.errors import AmigaFSError

try:
    AmigaImage.open(sys.argv[1])
except AmigaFSError as error:
    print(error)
    raise SystemExit(23)
raise SystemExit(0)
"""
    with AmigaImage.open(image_path, writable=True):
        result = subprocess.run(
            [sys.executable, "-c", script, str(image_path)],
            check=False,
            capture_output=True,
            text=True,
        )
    assert result.returncode == 23
    assert "another AmigaFS process" in result.stdout


def test_checkpoint_refuses_symlinked_identity_directory(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    before = image_path.read_bytes()
    identity = hashlib.sha256(
        str(image_path.resolve()).encode("utf-8", "surrogateescape")
    ).hexdigest()
    recovery_root = tmp_path / "state" / "amigafs" / "recovery"
    recovery_root.mkdir(parents=True)
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (recovery_root / identity).symlink_to(redirected, target_is_directory=True)

    with pytest.raises(AmigaFSError, match="unsafe private AmigaFS directory"):
        AmigaImage.open(image_path, writable=True)

    assert list(redirected.iterdir()) == []
    assert image_path.read_bytes() == before


def test_journal_is_never_written_through_a_planted_symbolic_link(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    identity = hashlib.sha256(
        str(image_path.resolve()).encode("utf-8", "surrogateescape")
    ).hexdigest()
    directory = tmp_path / "state" / "amigafs" / "recovery" / identity
    directory.mkdir(parents=True, mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="utf-8")
    os.chmod(tmp_path / "state", 0o700)

    real_unlink = Path.unlink

    def plant_after_cleanup(path: Path, missing_ok: bool = False) -> None:
        real_unlink(path, missing_ok=missing_ok)
        if path.name == "undo.journal" and not path.is_symlink():
            path.symlink_to(victim)

    with (
        patch("pathlib.Path.unlink", autospec=True, side_effect=plant_after_cleanup),
        pytest.raises(AmigaFSError, match="Could not create the writable recovery checkpoint"),
    ):
        AmigaImage.open(image_path, writable=True)
    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_mount_registry_refuses_symlinked_private_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amigafs.mounts import register_mount

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (runtime / "amigafs").symlink_to(redirected, target_is_directory=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    image_path = create_floppy(tmp_path)
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()

    with pytest.raises(AmigaFSError, match="unsafe private AmigaFS directory"):
        register_mount(image_path, mountpoint, read_write=False)

    assert list(redirected.iterdir()) == []


def test_preferences_refuse_symlinked_private_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amigafs.preferences import set_mount_location

    config = tmp_path / "config"
    config.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (config / "amigafs").symlink_to(redirected, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    with pytest.raises(AmigaFSError, match="unsafe private AmigaFS directory"):
        set_mount_location("sidebar")

    assert list(redirected.iterdir()) == []


def test_custom_mount_root_refuses_symbolic_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amigafs.preferences import ensure_mount_root

    redirected = tmp_path / "redirected"
    redirected.mkdir()
    mount_root = tmp_path / "mounts"
    mount_root.symlink_to(redirected, target_is_directory=True)
    monkeypatch.setenv("AMIGAFS_MOUNT_ROOT", str(mount_root))

    with pytest.raises(AmigaFSError, match="symbolic link"):
        ensure_mount_root()

    assert list(redirected.iterdir()) == []


def test_private_directory_creation_rejects_component_swap(
    tmp_path: Path,
) -> None:
    from amigafs.safe_paths import ensure_private_directory

    anchor = tmp_path / "anchor"
    anchor.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    real_mkdir = os.mkdir

    def swap_after_create(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        real_mkdir(path, mode, dir_fd=dir_fd)
        if path == "private" and dir_fd is not None:
            os.rename("private", "moved", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.symlink(redirected, "private", target_is_directory=True, dir_fd=dir_fd)

    with (
        patch("amigafs.safe_paths.os.mkdir", side_effect=swap_after_create),
        pytest.raises(AmigaFSError, match="unsafe private AmigaFS directory"),
    ):
        ensure_private_directory(anchor / "private" / "child", anchor=anchor)

    assert list(redirected.iterdir()) == []


def test_repair_audit_refuses_symlinked_private_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amigafs.core.repair import _write_audit

    state = tmp_path / "state"
    audit_parent = state / "amigafs"
    audit_parent.mkdir(parents=True)
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (audit_parent / "repair-audits").symlink_to(redirected, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))

    with pytest.raises(AmigaFSError, match="unsafe private AmigaFS directory"):
        _write_audit(audit_parent / "repair-audits" / "audit.json", {"status": "test"})

    assert list(redirected.iterdir()) == []


def test_repair_audit_does_not_follow_predictable_temporary_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amigafs.core.repair import _write_audit

    state = tmp_path / "state"
    audit = state / "amigafs" / "repair-audits" / "audit.json"
    redirected = tmp_path / "redirected"
    redirected.write_text("unchanged", encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    audit.parent.mkdir(parents=True)
    temporary = audit.with_name(f".{audit.name}.{'a' * 32}.tmp")
    temporary.symlink_to(redirected)

    with (
        patch("amigafs.core.repair.uuid.uuid4") as uuid4,
        pytest.raises(FileExistsError),
    ):
        uuid4.return_value.hex = "a" * 32
        _write_audit(audit, {"status": "test"})

    assert redirected.read_text(encoding="utf-8") == "unchanged"
    assert temporary.is_symlink()
    assert not audit.exists()


def test_desktop_child_environment_does_not_inherit_unrelated_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from amigafs.desktop import _desktop_environment

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    monkeypatch.setenv("UNRELATED_API_TOKEN", "secret")

    environment = _desktop_environment()

    assert environment["PATH"] == "/usr/bin"
    assert environment["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:")
    assert environment["AMIGAFS_DESKTOP_MOUNT"] == "1"
    assert "UNRELATED_API_TOKEN" not in environment


def test_unknown_user_image_reference_is_rejected_safely() -> None:
    from amigafs.desktop import local_image_reference

    with pytest.raises(AmigaFSError, match="unknown user account"):
        local_image_reference("~amigafs-user-that-does-not-exist/image.adf")


def test_malformed_image_uri_is_rejected_safely() -> None:
    from amigafs.desktop import local_image_reference

    with pytest.raises(AmigaFSError, match="URI is malformed"):
        local_image_reference("file://[invalid/image.adf")


@pytest.mark.parametrize("reference", ["image\0.adf", "file:///tmp/image%00.adf"])
def test_nul_image_reference_is_rejected_safely(reference: str) -> None:
    from amigafs.desktop import local_image_reference

    with pytest.raises(AmigaFSError, match="invalid path.*NUL"):
        local_image_reference(reference)


def test_user_visible_untrusted_detail_is_bounded_and_redacted() -> None:
    from amigafs.privacy import MAX_USER_MESSAGE_CHARS, safe_user_message

    rendered = safe_user_message("failed at /home/alice/Private Images/image.adf\0: " + "x" * 2000)

    assert "/home/alice" not in rendered
    assert "Private Images/image.adf" not in rendered
    assert "\0" not in rendered
    assert len(rendered) == MAX_USER_MESSAGE_CHARS
