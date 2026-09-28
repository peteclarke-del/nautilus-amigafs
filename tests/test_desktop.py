from __future__ import annotations

import os
import time
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

import pytest

from amigafs import desktop
from amigafs.core.image import ROOT_INODE, AmigaImage
from amigafs.desktop import (
    _last_log_line,
    _notify,
    _run_with_progress,
    _run_with_reported_progress,
    _systemd_mount_command,
    background_mount,
    cleanup_stale_mountpoint,
    desktop_claims,
    desktop_configure_mount_location,
    desktop_create,
    desktop_mount,
    desktop_mount_disc,
    desktop_mount_floppy,
    desktop_open,
    desktop_open_file_forge,
    desktop_read_disc,
    desktop_read_floppy,
    desktop_recover,
    desktop_repair,
    desktop_unmount,
    desktop_validate,
    desktop_write_disc,
    desktop_write_floppy,
    local_image_reference,
    mountpoint_for_image,
    shutdown_timeout,
)
from amigafs.errors import AmigaFSError, OperationCancelled
from amigafs.mounts import MountRecord
from amigafs.preferences import mount_location, preferences_path
from amigafs.recovery import pending_recovery
from tests.image_fixture import (
    corrupt_file_header,
    create_floppy,
    create_hard_disc,
    gzip_image,
    invalidate_bitmap,
)

ZENITY = "/usr/bin/zenity"


@pytest.fixture(autouse=True)
def run_progress_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "amigafs.desktop._run_with_progress",
        lambda _title, _message, operation: operation(lambda: False),
    )
    monkeypatch.setattr(
        "amigafs.desktop._run_with_reported_progress",
        lambda _title, _message, operation: operation(lambda _percent, _message: None),
    )
    monkeypatch.setattr(
        "amigafs.desktop._run_with_progress_and_cancel",
        lambda _title, _message, operation: operation(
            lambda _percent, _message: None, lambda: False
        ),
    )


def _text(arguments: list[str]) -> str:
    return next(item for item in arguments if item.startswith("--text="))


def _disc(name: str = "sdb", stable: str = "/dev/disk/by-id/usb-CF_Card") -> SimpleNamespace:
    return SimpleNamespace(
        name=name, stable_path=stable, model="CF Card", size=4_000_000_000, device=f"/dev/{name}"
    )


def test_mountpoint_is_stable_and_private_to_one_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount_root = tmp_path / "mounts"
    monkeypatch.setenv("AMIGAFS_MOUNT_ROOT", str(mount_root))
    image = create_floppy(tmp_path, name="Work.Bench")
    link = tmp_path / "alias.adf"
    link.symlink_to(image)
    assert mountpoint_for_image(image) == mountpoint_for_image(link)
    assert mountpoint_for_image(image).parent == mount_root
    assert mountpoint_for_image(image).name.startswith("Work.Bench-")
    other = create_floppy(
        tmp_path / "other" if (tmp_path / "other").mkdir() is None else tmp_path, name="Work.Bench"
    )
    assert mountpoint_for_image(other) != mountpoint_for_image(image)
    floppy = mountpoint_for_image("floppy:A")
    assert floppy.name.startswith("Floppy-drive-A-")
    assert floppy != mountpoint_for_image("floppy:B")


def test_changed_preference_reuses_an_existing_image_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = create_floppy(tmp_path)
    old_mountpoint = tmp_path / "old-root" / "floppy-existing"
    old_mountpoint.mkdir(parents=True)
    new_root = tmp_path / "new-root"
    monkeypatch.setenv("AMIGAFS_MOUNT_ROOT", str(new_root))
    record = MountRecord(
        str(old_mountpoint), image.name, "ro", image_path=str(image), read_write=False
    )

    with (
        patch("amigafs.desktop.cleanup_retained_state"),
        patch("amigafs.desktop.mount_for_image", return_value=record),
        patch("amigafs.desktop.mount_at", return_value=record),
        patch("amigafs.desktop.cleanup_stale_mountpoint") as cleanup,
        patch("amigafs.desktop.subprocess.Popen") as launch,
        patch("amigafs.desktop._open_folder") as open_folder,
        patch("amigafs.desktop._notify"),
    ):
        mounted = background_mount(image)

    assert mounted == old_mountpoint
    assert not new_root.exists()
    cleanup.assert_not_called()
    launch.assert_not_called()
    open_folder.assert_called_once_with(old_mountpoint)


def _mount_sequence(records: list[object | None]) -> Callable[[object], object | None]:
    remaining = list(records)

    def lookup(_image: object) -> object | None:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return lookup


def test_background_mount_starts_a_collected_user_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMIGAFS_MOUNT_ROOT", str(tmp_path / "mounts"))
    image = create_floppy(tmp_path)
    mounted = MountRecord("/x", image.name, "rw", read_write=True)
    launched = SimpleNamespace(returncode=0, stderr="")
    updates: list[int] = []
    with (
        patch("amigafs.desktop.cleanup_retained_state"),
        patch(
            "amigafs.desktop.mount_for_image", side_effect=_mount_sequence([None, None, mounted])
        ),
        patch("amigafs.desktop.mount_at", return_value=None),
        patch("amigafs.desktop.is_mounted", return_value=False),
        patch("amigafs.desktop._systemd_user_available", return_value=True),
        patch("amigafs.desktop._unit_active", return_value=True),
        patch("amigafs.desktop.subprocess.run", return_value=launched) as run,
        patch("amigafs.desktop._open_folder") as open_folder,
        patch("amigafs.desktop._notify") as notify,
    ):
        mountpoint = background_mount(
            image, read_write=True, progress=lambda percent, _text: updates.append(percent)
        )
    command = run.call_args.args[0]
    assert command[:2] == ["systemd-run", "--user"]
    assert command[-5:] == [
        "amigafs.cli",
        "mount",
        "--read-write",
        str(image),
        str(mountpoint),
    ]
    assert not any(item.startswith("--setenv=AMIGAFS_DEVICE_SOCKET") for item in command)
    assert mountpoint.is_dir() and mountpoint.stat().st_mode & 0o777 == 0o700
    open_folder.assert_called_once_with(mountpoint)
    notify.assert_called_once_with(
        "AmigaFS image mounted", f"{image.name} is available in Files (read-write)."
    )
    assert updates == sorted(updates) and updates[-1] == 100


def test_background_mount_without_systemd_runs_a_detached_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMIGAFS_MOUNT_ROOT", str(tmp_path / "mounts"))
    monkeypatch.setenv("SECRET_TOKEN", "do-not-inherit")
    image = create_floppy(tmp_path)
    mounted = MountRecord("/x", image.name, "ro", read_write=False)
    process = SimpleNamespace(poll=lambda: None, returncode=None, terminate=lambda: None)
    with (
        patch("amigafs.desktop.cleanup_retained_state"),
        patch(
            "amigafs.desktop.mount_for_image", side_effect=_mount_sequence([None, None, mounted])
        ),
        patch("amigafs.desktop.mount_at", return_value=None),
        patch("amigafs.desktop.is_mounted", return_value=False),
        patch("amigafs.desktop._systemd_user_available", return_value=False),
        patch("amigafs.desktop.subprocess.Popen", return_value=process) as popen,
        patch("amigafs.desktop._open_folder"),
        patch("amigafs.desktop._notify"),
    ):
        mountpoint = background_mount(image)
    assert popen.call_args.args[0][-3:] == ["mount", str(image), str(mountpoint)]
    environment = popen.call_args.kwargs["env"]
    assert environment["AMIGAFS_DESKTOP_MOUNT"] == "1"
    assert "SECRET_TOKEN" not in environment
    assert popen.call_args.kwargs["start_new_session"] is True


def test_floppy_mount_passes_the_drive_reference_and_waits_long_enough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMIGAFS_MOUNT_ROOT", str(tmp_path / "mounts"))
    mounted = MountRecord("/x", "Floppy_drive_A", "ro", read_write=False)
    launched = SimpleNamespace(returncode=0, stderr="")
    with (
        patch("amigafs.desktop.cleanup_retained_state"),
        patch(
            "amigafs.desktop.mount_for_image", side_effect=_mount_sequence([None, None, mounted])
        ),
        patch("amigafs.desktop.mount_at", return_value=None),
        patch("amigafs.desktop.is_mounted", return_value=False),
        patch("amigafs.desktop._systemd_user_available", return_value=True),
        patch("amigafs.desktop._unit_active", return_value=True),
        patch("amigafs.desktop.subprocess.run", return_value=launched) as run,
        patch("amigafs.desktop._mount_timeout", wraps=desktop._mount_timeout) as timeout,
        patch("amigafs.desktop._open_folder"),
        patch("amigafs.desktop._notify"),
    ):
        mountpoint = background_mount("floppy:a")
    assert run.call_args.args[0][-3:] == ["mount", "floppy:A", str(mountpoint)]
    assert timeout.call_args.args[0].kind == "physical-floppy"
    assert desktop._mount_timeout(timeout.call_args.args[0], read_write=False) == 35 * 60.0


def test_physical_disc_is_opened_in_the_session_and_handed_to_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amigafs.core.formats import ImageCapabilities, ResolvedImage

    monkeypatch.setenv("AMIGAFS_MOUNT_ROOT", str(tmp_path / "mounts"))
    backing = create_hard_disc(tmp_path, capacity="4MB")
    source = ResolvedImage(
        primary_path=backing,
        kind="physical-disc",
        capabilities=ImageCapabilities(True, True, True, True, True, True, False),
        is_device=True,
        display_name="CF Card",
    )
    mounted = MountRecord("/x", "CF_Card", "rw", read_write=True)
    launched = SimpleNamespace(returncode=0, stderr="")
    opened: list[bool] = []
    served: list[int] = []

    def open_device(_path: object, *, writable: bool) -> int:
        opened.append(writable)
        return os.open(backing, os.O_RDONLY)

    def serve(self: object, descriptor: int, *, timeout: float = 0.0) -> bool:
        served.append(descriptor)
        return True

    with (
        patch("amigafs.desktop.cleanup_retained_state"),
        patch("amigafs.desktop.resolve_image", return_value=source),
        patch("amigafs.desktop.open_device", side_effect=open_device),
        patch("amigafs.core.devices.DescriptorHandover.serve_once", serve),
        patch(
            "amigafs.desktop.mount_for_image", side_effect=_mount_sequence([None, None, mounted])
        ),
        patch("amigafs.desktop.mount_at", return_value=None),
        patch("amigafs.desktop.is_mounted", return_value=False),
        patch("amigafs.desktop._systemd_user_available", return_value=True),
        patch("amigafs.desktop._unit_active", return_value=True),
        patch("amigafs.desktop.subprocess.run", return_value=launched) as run,
        patch("amigafs.desktop._open_folder"),
        patch("amigafs.desktop._notify"),
    ):
        mountpoint = background_mount("/dev/sdb", read_write=True)
    assert opened == [True]
    assert len(served) == 1
    command = run.call_args.args[0]
    (socket_option,) = [item for item in command if "AMIGAFS_DEVICE_SOCKET" in item]
    socket_path = Path(socket_option.split("=", 2)[2])
    assert socket_path.name == f".{mountpoint.name}.device"
    # The socket and the launcher's descriptor are both gone once the daemon has its own.
    assert not socket_path.exists()
    with pytest.raises(OSError):
        os.fstat(served[0])


def test_refused_disc_access_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amigafs.core.formats import ImageCapabilities, ResolvedImage
    from amigafs.errors import DeviceAccessError

    monkeypatch.setenv("AMIGAFS_MOUNT_ROOT", str(tmp_path / "mounts"))
    source = ResolvedImage(
        primary_path=tmp_path / "sdb",
        kind="physical-disc",
        capabilities=ImageCapabilities(True, True, True, True, True, True, False),
        is_device=True,
    )
    with (
        patch("amigafs.desktop.cleanup_retained_state"),
        patch("amigafs.desktop.resolve_image", return_value=source),
        patch(
            "amigafs.desktop.open_device",
            side_effect=DeviceAccessError("Permission to open the disc was not granted."),
        ),
        patch("amigafs.desktop.mount_for_image", return_value=None),
        patch("amigafs.desktop.mount_at", return_value=None),
        patch("amigafs.desktop.is_mounted", return_value=False),
        patch("amigafs.desktop.subprocess.run") as run,
        patch("amigafs.desktop.subprocess.Popen") as popen,
        pytest.raises(DeviceAccessError, match="not granted"),
    ):
        background_mount("/dev/sdb")
    run.assert_not_called()
    popen.assert_not_called()


def test_a_service_that_exits_early_reports_its_last_log_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMIGAFS_MOUNT_ROOT", str(tmp_path / "mounts"))
    image = create_floppy(tmp_path)
    launched = SimpleNamespace(returncode=0, stderr="")
    with (
        patch("amigafs.desktop.cleanup_retained_state"),
        patch("amigafs.desktop.mount_for_image", return_value=None),
        patch("amigafs.desktop.mount_at", return_value=None),
        patch("amigafs.desktop.is_mounted", return_value=False),
        patch("amigafs.desktop._systemd_user_available", return_value=True),
        patch("amigafs.desktop._unit_active", return_value=False),
        patch("amigafs.desktop._unit_log_line", return_value="amigafs: the image is damaged"),
        patch("amigafs.desktop.subprocess.run", return_value=launched),
        pytest.raises(AmigaFSError, match="the image is damaged"),
    ):
        background_mount(image)


def test_read_only_formats_are_never_mounted_read_write(tmp_path: Path) -> None:
    archive = tmp_path / "game.dms"
    archive.write_bytes(b"DMS!" + bytes(64))
    with (
        patch("amigafs.desktop.cleanup_retained_state"),
        patch("amigafs.desktop.subprocess.run") as run,
        pytest.raises(AmigaFSError, match="not supported for this image format"),
    ):
        background_mount(archive, read_write=True)
    run.assert_not_called()


def test_desktop_uri_handler_accepts_only_local_image_paths() -> None:
    assert local_image_reference("file:///tmp/Amiga%20image/work.adf") == Path(
        "/tmp/Amiga image/work.adf"
    )
    assert local_image_reference("amigafs:///tmp/disk.hdf") == Path("/tmp/disk.hdf")
    assert local_image_reference("relative/work.adf") == Path("relative/work.adf")
    with pytest.raises(AmigaFSError, match="only local"):
        local_image_reference("file://server/share/work.adf")
    with pytest.raises(AmigaFSError, match="Unsupported"):
        local_image_reference("https://example.test/work.adf")
    with pytest.raises(AmigaFSError, match="Unsupported"):
        local_image_reference("acornfs:///tmp/scsi0.dat")
    with pytest.raises(AmigaFSError, match="invalid path"):
        local_image_reference("file:///tmp/work%00.adf")
    with pytest.raises(AmigaFSError, match="unambiguous"):
        local_image_reference("file:///tmp/work.adf?version=2")


def test_desktop_open_mounts_mime_references_read_only() -> None:
    with (
        patch("amigafs.desktop.desktop_mount", return_value=0) as mount,
        patch("amigafs.desktop.sibling_claiming", return_value=None),
    ):
        assert desktop_open(["file:///tmp/work.adf", "amigafs:///tmp/disk.hdf"]) == 0
    assert mount.call_args_list == [
        ((Path("/tmp/work.adf"),), {"read_write": False}),
        ((Path("/tmp/disk.hdf"),), {"read_write": False}),
    ]


def test_an_amiga_image_is_mounted_without_asking_a_sibling(tmp_path: Path) -> None:
    image = create_floppy(tmp_path)
    assert desktop_claims(image) == 0
    with (
        patch("amigafs.desktop.desktop_mount", return_value=0) as mount,
        patch("amigafs.desktop.sibling_claiming") as asked,
    ):
        assert desktop_open([str(image)]) == 0
    asked.assert_not_called()
    mount.assert_called_once_with(image, read_write=False)


def test_a_foreign_image_is_handed_to_the_sibling_that_claims_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = tmp_path / "acorn.adf"
    foreign.write_bytes(bytes(800 * 1024))
    assert desktop_claims(foreign) == 1
    monkeypatch.setenv("AMIGAFS_DEVICE_SOCKET", "/run/secret")
    with (
        patch("amigafs.desktop.desktop_mount", return_value=0) as mount,
        patch("amigafs.desktop.sibling_claiming", return_value="/usr/bin/acornfs") as asked,
        patch("amigafs.desktop.hand_off") as handed,
    ):
        assert desktop_open([foreign.as_uri()]) == 0
    mount.assert_not_called()
    (path, environment), _keywords = asked.call_args
    assert path == foreign
    assert "AMIGAFS_DEVICE_SOCKET" not in environment
    assert "AMIGAFS_DESKTOP_MOUNT" not in environment
    handed.assert_called_once_with("/usr/bin/acornfs", foreign, environment)


def test_an_unclaimed_or_handed_over_image_is_never_passed_on(tmp_path: Path) -> None:
    foreign = tmp_path / "unknown.adf"
    foreign.write_bytes(bytes(800 * 1024))
    with (
        patch("amigafs.desktop.desktop_mount", return_value=0) as mount,
        patch("amigafs.desktop.sibling_claiming", return_value=None),
        patch("amigafs.desktop.hand_off") as handed,
    ):
        assert desktop_open([str(foreign)]) == 0
    handed.assert_not_called()
    mount.assert_called_once_with(foreign, read_write=False)
    with (
        patch("amigafs.desktop.desktop_mount", return_value=0) as mount,
        patch("amigafs.desktop.sibling_claiming", return_value="/usr/bin/acornfs") as asked,
        patch("amigafs.desktop.hand_off") as handed,
    ):
        assert desktop_open([str(foreign)], handed_off=True) == 0
    asked.assert_not_called()
    handed.assert_not_called()
    mount.assert_called_once_with(foreign, read_write=False)


def test_desktop_open_notifies_for_refused_uri() -> None:
    with (
        patch("amigafs.desktop._notify") as notify,
        pytest.raises(AmigaFSError, match="Unsupported"),
    ):
        desktop_open(["https://example.test/work.adf"])
    notify.assert_called_once_with(
        "AmigaFS open failed", "Unsupported image URI scheme: https", error=True
    )


def test_desktop_mount_reports_progress_opens_folder_and_confirms_success() -> None:
    mountpoint = Path("/mounts/Example")
    with (
        patch("amigafs.desktop.background_mount", return_value=mountpoint) as mount,
        patch("amigafs.desktop._open_folder") as open_folder,
        patch("amigafs.desktop._show_desktop_message") as show,
    ):
        assert desktop_mount("/images/Example.adf", read_write=False) == 0

    assert mount.call_args.args == (Path("/images/Example.adf"),)
    assert mount.call_args.kwargs["open_folder"] is False
    assert mount.call_args.kwargs["notify"] is False
    assert mount.call_args.kwargs["read_write"] is False
    assert callable(mount.call_args.kwargs["progress"])
    open_folder.assert_called_once_with(mountpoint)
    show.assert_called_once_with(
        "AmigaFS image mounted",
        "Example.adf is available in Files at /mounts/Example (read-only).",
    )


def test_desktop_mount_names_a_floppy_by_its_drive() -> None:
    with (
        patch("amigafs.desktop.background_mount", return_value=Path("/mounts/F")) as mount,
        patch("amigafs.desktop._open_folder"),
        patch("amigafs.desktop._show_desktop_message") as show,
    ):
        assert desktop_mount("floppy:b", read_write=True) == 0
    assert mount.call_args.args == ("floppy:b",)
    assert show.call_args.args[1] == (
        "Floppy drive B is available in Files at /mounts/F (read-write)."
    )


def test_desktop_mount_shows_actionable_failure_dialog() -> None:
    with (
        patch("amigafs.desktop.background_mount", side_effect=AmigaFSError("bad image")),
        patch("amigafs.desktop._show_desktop_message") as show,
        pytest.raises(AmigaFSError, match="bad image"),
    ):
        desktop_mount("/images/Example.adf")

    show.assert_called_once_with("AmigaFS mount failed", "bad image", error=True)


def test_desktop_file_forge_handoff_reports_launcher_failure() -> None:
    with (
        patch("amigafs.desktop.open_in_file_forge", side_effect=AmigaFSError("not installed")),
        patch("amigafs.desktop._show_desktop_message") as show,
        pytest.raises(AmigaFSError, match="not installed"),
    ):
        desktop_open_file_forge("/image.adf")
    show.assert_called_once_with("Could not open Amiga File Forge", "not installed", error=True)


def test_desktop_floppy_write_selects_confirms_writes_and_reports_success() -> None:
    with (
        patch("amigafs.desktop.responsive_command", return_value="/usr/bin/gw"),
        patch("amigafs.desktop.detected_drives", return_value=("B",)),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="B\n"),
                SimpleNamespace(returncode=0),
            ],
        ) as run,
        patch(
            "amigafs.desktop.write_floppy",
            return_value=SimpleNamespace(drive="B", verified=True),
        ) as write,
        patch("amigafs.desktop._show_desktop_message") as show,
    ):
        assert desktop_write_floppy("/images/private-name.adf") == 0

    write.assert_called_once_with(Path("/images/private-name.adf"), "B", progress=ANY)
    confirmation_arguments = run.call_args_list[1].args[0]
    selection_arguments = run.call_args_list[0].args[0]
    assert "--combo-values=B" in selection_arguments
    assert "--no-markup" in confirmation_arguments
    assert "--ok-label=Overwrite and verify" in confirmation_arguments
    assert "--cancel-label=Cancel" in confirmation_arguments
    assert "drive B will be overwritten" in _text(confirmation_arguments)
    show.assert_called_once_with(
        "Physical floppy complete",
        "Greaseweazle wrote and verified private-name.adf in drive B.",
    )


def test_desktop_floppy_write_explains_that_flux_images_are_not_verified() -> None:
    with (
        patch("amigafs.desktop.responsive_command", return_value="/usr/bin/gw"),
        patch("amigafs.desktop.detected_drives", return_value=("A",)),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="A\n"),
                SimpleNamespace(returncode=0),
            ],
        ),
        patch(
            "amigafs.desktop.write_floppy",
            return_value=SimpleNamespace(drive="A", verified=False),
        ),
        patch("amigafs.desktop._show_desktop_message") as show,
    ):
        assert desktop_write_floppy("/images/protected.ipf") == 0
    assert "cannot be verified by reading it back" in show.call_args.args[1]


def test_desktop_floppy_write_confirmation_can_be_cancelled() -> None:
    with (
        patch("amigafs.desktop.responsive_command", return_value="/usr/bin/gw"),
        patch("amigafs.desktop.detected_drives", return_value=("A",)),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="A\n"),
                SimpleNamespace(returncode=1),
            ],
        ),
        patch("amigafs.desktop.write_floppy") as write,
    ):
        assert desktop_write_floppy("disk.adf") == 0

    write.assert_not_called()


def test_desktop_floppy_actions_stop_when_no_drive_or_device_is_detected() -> None:
    with (
        patch("amigafs.desktop.responsive_command", return_value="/usr/bin/gw"),
        patch("amigafs.desktop.detected_drives", return_value=()),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch("amigafs.desktop._show_desktop_message") as show,
        patch("amigafs.desktop.write_floppy") as write,
        pytest.raises(AmigaFSError, match="No physical drive"),
    ):
        desktop_write_floppy("disk.adf")
    write.assert_not_called()
    show.assert_called_once()
    with (
        patch("amigafs.desktop.responsive_command", return_value=None),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch("amigafs.desktop._show_desktop_message"),
        patch("amigafs.desktop.desktop_mount") as mount,
        pytest.raises(AmigaFSError, match="No responsive Greaseweazle"),
    ):
        desktop_mount_floppy()
    mount.assert_not_called()


def test_a_forged_drive_selection_is_rejected() -> None:
    with (
        patch("amigafs.desktop.responsive_command", return_value="/usr/bin/gw"),
        patch("amigafs.desktop.detected_drives", return_value=("A",)),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="B; reboot\n"),
        ),
        patch("amigafs.desktop.write_floppy") as write,
        pytest.raises(AmigaFSError, match="invalid response"),
    ):
        desktop_write_floppy("disk.adf")
    write.assert_not_called()


def test_desktop_floppy_mount_warns_before_a_read_write_session() -> None:
    with (
        patch("amigafs.desktop.responsive_command", return_value="/usr/bin/gw"),
        patch("amigafs.desktop.detected_drives", return_value=("A", "B")),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="B\n"),
                SimpleNamespace(returncode=0),
            ],
        ) as run,
        patch("amigafs.desktop.desktop_mount", return_value=0) as mount,
    ):
        assert desktop_mount_floppy(read_write=True) == 0
    mount.assert_called_once_with("floppy:B", read_write=True)
    warning = _text(run.call_args_list[1].args[0])
    assert "written back to the floppy" in warning
    assert "Leave the floppy in the drive" in warning
    with (
        patch("amigafs.desktop.responsive_command", return_value="/usr/bin/gw"),
        patch("amigafs.desktop.detected_drives", return_value=("A",)),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="A\n"),
                SimpleNamespace(returncode=1),
            ],
        ),
        patch("amigafs.desktop.desktop_mount") as declined,
    ):
        assert desktop_mount_floppy(read_write=True) == 0
    declined.assert_not_called()
    with (
        patch("amigafs.desktop.responsive_command", return_value="/usr/bin/gw"),
        patch("amigafs.desktop.detected_drives", return_value=("A",)),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="A\n"),
        ) as run,
        patch("amigafs.desktop.desktop_mount", return_value=0) as read_only,
    ):
        assert desktop_mount_floppy() == 0
    # A read-only floppy mount needs no warning.
    assert run.call_count == 1
    read_only.assert_called_once_with("floppy:A", read_write=False)


def test_desktop_floppy_read_collects_a_name_and_density(tmp_path: Path) -> None:
    from amigafs.core.containers import FLOPPY_FORMATS

    captured = SimpleNamespace(path=tmp_path / "game.adf", floppy_format=FLOPPY_FORMATS[1])
    with (
        patch("amigafs.desktop.responsive_command", return_value="/usr/bin/gw"),
        patch("amigafs.desktop.detected_drives", return_value=("A",)),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="A\n"),
                SimpleNamespace(returncode=0, stdout="game\x1fdd\n"),
            ],
        ),
        patch("amigafs.desktop.read_floppy", return_value=captured) as read,
        patch("amigafs.desktop._show_desktop_message") as show,
    ):
        assert desktop_read_floppy(tmp_path) == 0
    read.assert_called_once_with(tmp_path / "game.adf", "A", density="dd", progress=ANY)
    show.assert_called_once_with(
        "Physical floppy read", "Saved a complete Amiga DD, 880 KiB image as game.adf."
    )
    with (
        patch("amigafs.desktop.responsive_command", return_value="/usr/bin/gw"),
        patch("amigafs.desktop.detected_drives", return_value=("A",)),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="A\n"),
                SimpleNamespace(returncode=0, stdout="../escape\x1f\n"),
            ],
        ),
        patch("amigafs.desktop.read_floppy") as refused,
        pytest.raises(AmigaFSError, match="not a path"),
    ):
        desktop_read_floppy(tmp_path)
    refused.assert_not_called()


def test_desktop_disc_mount_lists_only_eligible_discs() -> None:
    discs = [_disc(), _disc("mmcblk0", "/dev/disk/by-id/mmc-SD_Card")]
    with (
        patch("amigafs.desktop.list_discs", return_value=discs),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="/dev/disk/by-id/mmc-SD_Card\n"),
        ) as run,
        patch("amigafs.desktop.desktop_mount", return_value=0) as mount,
    ):
        assert desktop_mount_disc(read_write=True) == 0
    mount.assert_called_once_with("/dev/disk/by-id/mmc-SD_Card", read_write=True)
    arguments = run.call_args.args[0]
    assert arguments[:2] == [ZENITY, "--list"]
    assert "--hide-column=1" in arguments and "--print-column=1" in arguments
    assert "CF Card" in arguments and "3.7 GiB" in arguments
    assert "never listed" in _text(arguments)


def test_desktop_disc_actions_reject_forged_or_missing_discs() -> None:
    with (
        patch("amigafs.desktop.list_discs", return_value=[_disc()]),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="/dev/sda\n"),
        ),
        patch("amigafs.desktop.desktop_mount") as mount,
        pytest.raises(AmigaFSError, match="invalid response"),
    ):
        desktop_mount_disc()
    mount.assert_not_called()
    with (
        patch("amigafs.desktop.list_discs", return_value=[]),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch("amigafs.desktop._show_desktop_message") as show,
        pytest.raises(AmigaFSError, match="No removable or USB disc"),
    ):
        desktop_mount_disc()
    show.assert_called_once()
    with (
        patch("amigafs.desktop.list_discs", return_value=[_disc()]),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="")
        ),
        patch("amigafs.desktop.desktop_mount") as cancelled,
    ):
        assert desktop_mount_disc() == 0
    cancelled.assert_not_called()


def test_desktop_disc_read_saves_a_new_image(tmp_path: Path) -> None:
    result = SimpleNamespace(size=4_000_000_000, image=tmp_path / "card.hdf")
    with (
        patch("amigafs.desktop.list_discs", return_value=[_disc()]),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="/dev/disk/by-id/usb-CF_Card\n"),
                SimpleNamespace(returncode=0, stdout="card.hdf\n"),
            ],
        ),
        patch("amigafs.desktop.read_disc", return_value=result) as read,
        patch("amigafs.desktop._show_desktop_message") as show,
    ):
        assert desktop_read_disc(tmp_path) == 0
    read.assert_called_once_with(
        "/dev/disk/by-id/usb-CF_Card", tmp_path / "card.hdf", progress=ANY, cancelled=ANY
    )
    show.assert_called_once_with("Physical disc read", "Saved 3.7 GiB as card.hdf.")
    with (
        patch("amigafs.desktop.list_discs", return_value=[_disc()]),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="/dev/disk/by-id/usb-CF_Card\n"),
                SimpleNamespace(returncode=0, stdout="card.hdf\n"),
            ],
        ),
        patch("amigafs.desktop.read_disc", side_effect=OperationCancelled("stopped")),
        patch("amigafs.desktop._notify") as notify,
    ):
        assert desktop_read_disc(tmp_path) == 0
    notify.assert_called_once_with("AmigaFS disc read cancelled", "No image was created.")


def test_desktop_disc_write_requires_the_typed_device_name(tmp_path: Path) -> None:
    image = tmp_path / "system.hdf"
    with (
        patch("amigafs.desktop.list_discs", return_value=[_disc()]),
        patch("amigafs.desktop.os.path.realpath", return_value="/dev/sdb"),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="/dev/disk/by-id/usb-CF_Card\n"),
                SimpleNamespace(returncode=0, stdout="sdb\n"),
            ],
        ) as run,
        patch("amigafs.desktop.write_disc") as write,
        patch("amigafs.desktop._show_desktop_message") as show,
    ):
        assert desktop_write_disc(image) == 0
    write.assert_called_once_with(
        image, "/dev/disk/by-id/usb-CF_Card", confirmation="sdb", progress=ANY
    )
    prompt = _text(run.call_args_list[1].args[0])
    assert "EVERYTHING on usb-CF_Card will be replaced" in prompt
    assert "Type sdb to confirm" in prompt
    show.assert_called_once_with(
        "Physical disc complete", "system.hdf was written to the disc and verified."
    )
    with (
        patch("amigafs.desktop.list_discs", return_value=[_disc()]),
        patch("amigafs.desktop.os.path.realpath", return_value="/dev/sdb"),
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="/dev/disk/by-id/usb-CF_Card\n"),
                SimpleNamespace(returncode=1, stdout=""),
            ],
        ),
        patch("amigafs.desktop.write_disc") as cancelled,
    ):
        assert desktop_write_disc(image) == 0
    cancelled.assert_not_called()


@pytest.mark.parametrize(
    ("kind", "form", "created", "expected"),
    [
        ("floppy", "games\x1fGames\x1fhd\x1fFFS-INTL\x1fyes\n", "games.adf", 1_802_240),
        ("floppy", "\x1f\x1f\x1f\x1f\n", "blank.adf", 901_120),
        ("hard-disc", "drive\x1fSystem\x1f8MB\x1f2\x1fPFS3\n", "drive.hdf", None),
        ("hard-disc", "\x1f\x1f4MB\x1f\x1f\n", "harddisk.hdf", None),
    ],
)
def test_desktop_create_collects_settings_and_reports_success(
    tmp_path: Path, kind: str, form: str, created: str, expected: int | None
) -> None:
    completed = SimpleNamespace(returncode=0)
    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            side_effect=[SimpleNamespace(returncode=0, stdout=form), completed],
        ) as run,
    ):
        assert desktop_create(tmp_path, kind=kind) == 0

    assert (tmp_path / created).is_file()
    if expected is not None:
        assert (tmp_path / created).stat().st_size == expected
    form_arguments = run.call_args_list[0].args[0]
    assert form_arguments[:2] == [ZENITY, "--forms"]
    assert "--ok-label=Create" in form_arguments
    result_arguments = run.call_args_list[1].args[0]
    assert result_arguments[:2] == [ZENITY, "--info"]
    assert created in _text(result_arguments)
    with AmigaImage.open(tmp_path / created) as image:
        assert image.integrity_report().findings == ()


def test_desktop_create_cancellation_and_bad_settings_create_nothing(tmp_path: Path) -> None:
    cancelled = SimpleNamespace(returncode=1, stdout="")
    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch("amigafs.desktop.subprocess.run", return_value=cancelled),
    ):
        assert desktop_create(tmp_path) == 0
    bad = SimpleNamespace(returncode=0, stdout="drive\x1fSystem\x1f8MB\x1fmany\x1fFFS\n")
    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run", side_effect=[bad, SimpleNamespace(returncode=0)]
        ) as run,
        pytest.raises(AmigaFSError, match="whole number"),
    ):
        desktop_create(tmp_path, kind="hard-disc")
    assert run.call_args_list[1].args[0][:2] == [ZENITY, "--error"]
    assert list(tmp_path.iterdir()) == []


def test_actions_that_need_a_dialog_explain_the_terminal_alternative(tmp_path: Path) -> None:
    with patch("amigafs.desktop.shutil.which", return_value=None):
        for action, fragment in (
            (lambda: desktop_create(tmp_path), "amigafs create-floppy"),
            (lambda: desktop_write_floppy("disk.adf"), "Zenity is required"),
            (lambda: desktop_read_floppy(tmp_path), "amigafs read-floppy"),
            (lambda: desktop_mount_floppy(), "amigafs mount floppy:A"),
            (lambda: desktop_mount_disc(), "amigafs mount /dev/DISC"),
            (lambda: desktop_read_disc(tmp_path), "amigafs read-disc"),
            (lambda: desktop_write_disc("disk.hdf"), "amigafs write-disc"),
            (lambda: desktop_recover("disk.adf"), "amigafs recover IMAGE"),
            (lambda: desktop_configure_mount_location(), "amigafs config-mount-location"),
        ):
            with pytest.raises(AmigaFSError, match=fragment):
                action()


def test_desktop_mount_location_is_saved(tmp_path: Path) -> None:
    target = tmp_path / "mounts"
    entry = SimpleNamespace(returncode=0, stdout=f"{target}\n")
    completed = SimpleNamespace(returncode=0)
    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch("amigafs.desktop.subprocess.run", side_effect=[entry, completed]) as run,
    ):
        assert desktop_configure_mount_location() == 0

    assert mount_location().root == target
    entry_arguments = run.call_args_list[0].args[0]
    assert entry_arguments[:2] == [ZENITY, "--entry"]
    assert "--ok-label=Save" in entry_arguments
    result_arguments = run.call_args_list[1].args[0]
    assert result_arguments[:2] == [ZENITY, "--info"]
    assert str(target) in _text(result_arguments)


def test_notification_and_log_details_redact_unrelated_paths(tmp_path: Path) -> None:
    log = tmp_path / "mount.log"
    log.write_text("failed at /home/alice/private/token\0\n", encoding="utf-8")
    with (
        patch("amigafs.desktop.shutil.which", return_value="/usr/bin/notify-send"),
        patch("amigafs.desktop.subprocess.run") as run,
    ):
        _notify("AmigaFS failed", "failed at /home/alice/private/token\0", error=True)

    arguments = run.call_args.args[0]
    assert "/home/alice" not in arguments[-1]
    assert "\0" not in arguments[-1]
    assert "/home/alice" not in _last_log_line(log)


def test_desktop_mount_location_can_replace_corrupt_preference() -> None:
    preferences_path().parent.mkdir(parents=True)
    preferences_path().write_text("broken", encoding="utf-8")
    entry = SimpleNamespace(returncode=0, stdout="sidebar\n")
    completed = SimpleNamespace(returncode=0)

    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch("amigafs.desktop.subprocess.run", side_effect=[entry, completed]) as run,
    ):
        assert desktop_configure_mount_location() == 0

    assert "saved preference is invalid" in _text(run.call_args_list[0].args[0])
    assert mount_location().mode == "sidebar"


def _interrupt(image_path: Path) -> bytes:
    original = image_path.read_bytes()
    image = AmigaImage.open(image_path, writable=True)
    image.create_file(ROOT_INODE, b"Interrupted")
    image.store.handle.close()
    return original


def test_desktop_recovery_restores_after_an_explicit_choice(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    original = _interrupt(image_path)
    choice = SimpleNamespace(returncode=0, stdout="Restore image to the pre-mount checkpoint\n")
    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch("amigafs.desktop.subprocess.run", return_value=choice) as run,
    ):
        assert desktop_recover(image_path) == 0
    assert image_path.read_bytes() == original
    assert pending_recovery(image_path) is None
    choice_arguments = run.call_args_list[0].args[0]
    assert choice_arguments[:3] == [ZENITY, "--list", "--radiolist"]
    assert "--ok-label=Continue" in choice_arguments
    assert "--cancel-label=Cancel" in choice_arguments
    assert run.call_args_list[1].args[0][:2] == [ZENITY, "--info"]


def test_desktop_recovery_can_keep_the_current_image_or_be_cancelled(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    _interrupt(image_path)
    current = image_path.read_bytes()
    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="")
        ),
    ):
        assert desktop_recover(image_path) == 0
    assert pending_recovery(image_path) is not None
    keep = SimpleNamespace(
        returncode=0, stdout="Keep the current image and discard the checkpoint\n"
    )
    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch("amigafs.desktop.subprocess.run", return_value=keep),
    ):
        assert desktop_recover(image_path) == 0
    assert image_path.read_bytes() == current
    assert pending_recovery(image_path) is None


def test_desktop_recovery_with_nothing_pending_says_so(tmp_path: Path) -> None:
    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch("amigafs.desktop.subprocess.run") as run,
        patch("amigafs.desktop._notify") as notify,
    ):
        assert desktop_recover(create_floppy(tmp_path)) == 0
    run.assert_not_called()
    notify.assert_called_once_with("AmigaFS recovery", "No recovery checkpoint is pending.")


def test_desktop_recovery_failure_is_shown_explicitly(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    _interrupt(image_path)
    choice = SimpleNamespace(returncode=0, stdout="Restore image to the pre-mount checkpoint\n")
    shown = SimpleNamespace(returncode=0)
    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch("amigafs.desktop.subprocess.run", side_effect=[choice, shown]) as run,
        patch(
            "amigafs.desktop.recover_image",
            side_effect=AmigaFSError("The image is still mounted."),
        ),
        pytest.raises(AmigaFSError, match="still mounted"),
    ):
        desktop_recover(image_path)
    error_arguments = run.call_args_list[1].args[0]
    assert error_arguments[:2] == [ZENITY, "--error"]
    assert "still mounted" in _text(error_arguments)


def test_interrupted_working_copy_can_be_salvaged_from_the_desktop(tmp_path: Path) -> None:
    packed = gzip_image(create_floppy(tmp_path), tmp_path / "disk.adz")
    before = packed.read_bytes()
    session = AmigaImage.open(packed, writable=True)
    node = session.create_file(ROOT_INODE, b"Unsaved")
    session.replace_file(node.inode, b"rescued")
    session.close(clean=False)
    saved = tmp_path / "rescued.adf"
    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            side_effect=[
                SimpleNamespace(
                    returncode=0, stdout="Save the interrupted working copy as a new image…\n"
                ),
                SimpleNamespace(returncode=0, stdout=f"{saved}\n"),
                SimpleNamespace(returncode=0),
            ],
        ) as run,
    ):
        assert desktop_recover(packed) == 0
    assert "source is unchanged" in _text(run.call_args_list[0].args[0])
    assert run.call_args_list[1].args[0][:3] == [ZENITY, "--file-selection", "--save"]
    assert packed.read_bytes() == before
    assert pending_recovery(packed) is None
    with AmigaImage.open(saved) as rescued:
        assert rescued.read(rescued.node_at_path("Unsaved").inode, 0, 9) == b"rescued"


def test_desktop_repair_requires_typed_filename_and_rebuilds_the_bitmap(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    good = image_path.read_bytes()
    invalidate_bitmap(image_path)
    confirmation = SimpleNamespace(returncode=0, stdout=f"{image_path.name}\n")

    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch("amigafs.desktop.subprocess.run", return_value=confirmation) as run,
        patch("amigafs.desktop._notify") as notify,
    ):
        assert desktop_repair(image_path) == 0

    repair_arguments = run.call_args_list[0].args[0]
    assert repair_arguments[:2] == [ZENITY, "--entry"]
    assert "--ok-label=Apply repair" in repair_arguments
    assert "--cancel-label=Cancel" in repair_arguments
    assert "Rebuild the block-allocation bitmap" in _text(repair_arguments)
    assert f"Type {image_path.name} to confirm" in _text(repair_arguments)
    assert run.call_args_list[1].args[0][:2] == [ZENITY, "--info"]
    assert image_path.read_bytes() == good
    notify.assert_not_called()


def test_desktop_repair_refuses_wrong_confirmation_and_unrepairable_damage(
    tmp_path: Path,
) -> None:
    image_path = create_floppy(tmp_path)
    invalidate_bitmap(image_path)
    damaged = image_path.read_bytes()
    wrong = SimpleNamespace(returncode=0, stdout="yes\n")
    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run", side_effect=[wrong, SimpleNamespace(returncode=0)]
        ) as run,
        pytest.raises(AmigaFSError, match="must exactly match"),
    ):
        desktop_repair(image_path)
    assert run.call_args_list[1].args[0][:2] == [ZENITY, "--error"]
    assert image_path.read_bytes() == damaged

    broken = create_floppy(tmp_path, name="broken")
    corrupt_file_header(broken, "C/List")
    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run", return_value=SimpleNamespace(returncode=0)
        ) as report,
    ):
        assert desktop_repair(broken) == 1
    assert report.call_args.args[0][:2] == [ZENITY, "--text-info"]
    with patch("amigafs.desktop._notify") as notify:
        assert desktop_repair(create_floppy(tmp_path, name="clean")) == 0
    notify.assert_called_once_with("AmigaFS repair", "clean.adf needs no repair.")


def test_dead_fuse_endpoint_is_detached_before_mounting(tmp_path: Path) -> None:
    target = tmp_path / "stale"
    target.mkdir()
    detached = SimpleNamespace(returncode=0, stderr="")
    with (
        patch("amigafs.desktop.is_mounted", return_value=True),
        patch("amigafs.desktop.os.listdir", side_effect=OSError(107, "not connected")),
        patch("amigafs.desktop.subprocess.run", return_value=detached) as run,
    ):
        assert cleanup_stale_mountpoint(target)
    run.assert_called_once_with(
        ["fusermount3", "-u", "-z", str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    with (
        patch("amigafs.desktop.is_mounted", return_value=True),
        patch("amigafs.desktop.os.listdir", return_value=["healthy"]),
        patch("amigafs.desktop.subprocess.run") as untouched,
    ):
        assert not cleanup_stale_mountpoint(target)
    untouched.assert_not_called()


def test_systemd_mount_uses_graceful_sigint_and_collection(tmp_path: Path) -> None:
    command = _systemd_mount_command("amigafs-test.service", ["python", "-m", "amigafs.cli"])
    assert command[:5] == [
        "systemd-run",
        "--user",
        "--quiet",
        "--collect",
        "--unit=amigafs-test.service",
    ]
    assert "--property=KillSignal=SIGINT" in command
    assert "--property=TimeoutStopSec=30s" in command
    assert "--setenv=AMIGAFS_DESKTOP_MOUNT=1" in command
    assert command[-3:] == ["python", "-m", "amigafs.cli"]
    with_socket = _systemd_mount_command(
        "amigafs-test.service", ["python"], device_socket=tmp_path / "handover"
    )
    assert f"--setenv=AMIGAFS_DEVICE_SOCKET={tmp_path / 'handover'}" in with_socket
    # A floppy needs time to be written back before its service may be stopped.
    patient = _systemd_mount_command(
        "amigafs-test.service", ["python"], stop_timeout=shutdown_timeout("physical-floppy")
    )
    assert "--property=TimeoutStopSec=2100s" in patient


def test_desktop_validation_reports_clean_image(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    with patch("amigafs.desktop._notify") as notify:
        assert desktop_validate(image_path) == 0
    notify.assert_called_once_with(
        "AmigaFS validation passed",
        "floppy.adf has no reported filesystem problems.",
    )


def test_desktop_validation_reports_safe_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = create_floppy(tmp_path)
    monkeypatch.setattr(
        "amigafs.desktop._run_with_progress",
        lambda *_args: (_ for _ in ()).throw(OperationCancelled("cancelled")),
    )
    with patch("amigafs.desktop._notify") as notify:
        assert desktop_validate(image_path) == 0
    notify.assert_called_once_with("AmigaFS validation cancelled", "The image was not modified.")


def test_progress_dialog_requests_cooperative_cancellation() -> None:
    class ClosedProgress:
        stdin = None

        @staticmethod
        def poll() -> int:
            return 1

    def operation(cancelled: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 1
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(0.001)
        if not cancelled():
            raise AssertionError("progress cancellation was not propagated")
        raise OperationCancelled("cancelled safely")

    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch("amigafs.desktop.subprocess.Popen", return_value=ClosedProgress()) as popen,
        pytest.raises(OperationCancelled, match="cancelled safely"),
    ):
        _run_with_progress("Title", "Working…", operation)

    arguments = popen.call_args.args[0]
    assert arguments[:3] == [ZENITY, "--progress", "--pulsate"]
    assert "--cancel-label=Cancel safely" in arguments


def test_repair_progress_dialog_receives_determinate_updates() -> None:
    class RecordingInput(StringIO):
        def close(self) -> None:
            pass

    class ProgressProcess:
        def __init__(self) -> None:
            self.stdin = RecordingInput()
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: int) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = -15

    process = ProgressProcess()

    def operation(progress: Callable[[int, str], None]) -> str:
        progress(10, "Planning repair")
        progress(55, "Writing the journal")
        progress(100, "Repair verified")
        return "done"

    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch("amigafs.desktop.subprocess.Popen", return_value=process) as popen,
    ):
        assert _run_with_reported_progress("Repair", "Starting", operation) == "done"

    arguments = popen.call_args.args[0]
    assert arguments[:2] == [ZENITY, "--progress"]
    assert "--percentage=0" in arguments
    assert "--no-cancel" in arguments
    assert "--pulsate" not in arguments
    assert process.stdin.getvalue() == (
        "#Planning repair\n10\n#Writing the journal\n55\n#Repair verified\n100\n"
    )


def test_desktop_validation_shows_finite_problem_report(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    corrupt_file_header(image_path, "Docs/ReadMe")
    dialog_result = SimpleNamespace(returncode=0)
    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch("amigafs.desktop.subprocess.run", return_value=dialog_result) as run,
        patch("amigafs.desktop._notify") as notify,
    ):
        assert desktop_validate(image_path) == 1

    arguments = run.call_args.args[0]
    assert arguments[:2] == [ZENITY, "--text-info"]
    assert "--ok-label=Close" in arguments
    assert "--no-cancel" in arguments
    height = int(next(item.split("=", 1)[1] for item in arguments if item.startswith("--height=")))
    assert height <= 480
    assert "volume.structure" in run.call_args.kwargs["input"]
    notify.assert_not_called()


def test_validation_dialog_offers_repair_for_a_damaged_bitmap(tmp_path: Path) -> None:
    image_path = create_floppy(tmp_path)
    good = image_path.read_bytes()
    invalidate_bitmap(image_path)
    report_choice = SimpleNamespace(returncode=0)
    confirmation = SimpleNamespace(returncode=0, stdout=f"{image_path.name}\n")
    completed = SimpleNamespace(returncode=0)

    with (
        patch("amigafs.desktop.shutil.which", return_value=ZENITY),
        patch(
            "amigafs.desktop.subprocess.run",
            side_effect=[report_choice, confirmation, completed],
        ) as run,
        patch("amigafs.desktop._notify") as notify,
    ):
        assert desktop_validate(image_path) == 0

    report_arguments = run.call_args_list[0].args[0]
    assert report_arguments[:2] == [ZENITY, "--text-info"]
    assert "--ok-label=Repair…" in report_arguments
    assert "--cancel-label=Cancel" in report_arguments
    assert "--no-cancel" not in report_arguments
    assert run.call_args_list[1].args[0][:2] == [ZENITY, "--entry"]
    assert run.call_args_list[2].args[0][:2] == [ZENITY, "--info"]
    assert image_path.read_bytes() == good
    notify.assert_not_called()


@pytest.mark.parametrize(
    ("kind", "summary", "timeout"),
    [
        ("floppy-image", "{name} was flushed and validated safely.", 120.0),
        ("compressed-image", "{name} was flushed and validated safely.", 600.0),
        (
            "physical-floppy",
            "{name} was written and validated. The disc can be removed.",
            2100.0,
        ),
        (
            "physical-disc",
            "{name} was written and validated. The disc can be removed.",
            120.0,
        ),
    ],
)
def test_writable_desktop_unmount_waits_for_safe_finalisation(
    tmp_path: Path, kind: str, summary: str, timeout: float
) -> None:
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    record = MountRecord(
        mountpoint=str(mountpoint),
        source="disk.adf",
        options="rw",
        image_path=str(tmp_path / "disk.adf"),
        image_kind=kind,
        read_write=True,
    )
    result = SimpleNamespace(returncode=0, stderr="")
    with (
        patch("amigafs.desktop.mount_at", return_value=record),
        patch("amigafs.desktop.wait_for_mount_shutdown", side_effect=[False, True]) as wait,
        patch("amigafs.desktop.pending_recovery", return_value=None),
        patch("amigafs.desktop.subprocess.run", return_value=result) as run,
        patch("amigafs.desktop._notify") as notify,
    ):
        assert desktop_unmount(mountpoint) == 0

    assert run.call_args.args[0] == ["fusermount3", "-u", str(mountpoint)]
    assert wait.call_args_list[-1].kwargs == {"timeout": timeout}
    assert shutdown_timeout(kind) == timeout
    notify.assert_called_once_with("AmigaFS image unmounted", summary.format(name=mountpoint.name))
    assert not mountpoint.exists()


def test_unconfirmed_or_unfinalised_unmount_is_never_reported_as_safe(tmp_path: Path) -> None:
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    record = MountRecord(
        str(mountpoint),
        "Floppy_drive_A",
        "rw",
        image_path=str(tmp_path / "drive-A"),
        image_kind="physical-floppy",
        read_write=True,
    )
    result = SimpleNamespace(returncode=0, stderr="")
    with (
        patch("amigafs.desktop.mount_at", return_value=record),
        patch("amigafs.desktop.wait_for_mount_shutdown", return_value=False),
        patch("amigafs.desktop.subprocess.run", return_value=result),
        patch("amigafs.desktop._show_desktop_message") as show,
        patch("amigafs.desktop._notify") as notify,
        pytest.raises(AmigaFSError, match="remove the disc"),
    ):
        desktop_unmount(mountpoint)
    assert show.call_args.args[0] == "AmigaFS unmount not confirmed"
    notify.assert_not_called()
    with (
        patch("amigafs.desktop.mount_at", return_value=record),
        patch("amigafs.desktop.wait_for_mount_shutdown", return_value=True),
        patch("amigafs.desktop.pending_recovery", return_value=object()),
        patch("amigafs.desktop.subprocess.run", return_value=result),
        patch("amigafs.desktop._show_desktop_message") as show,
        pytest.raises(AmigaFSError, match="not finalised safely"),
    ):
        desktop_unmount(mountpoint)
    assert show.call_args.args[0] == "AmigaFS final validation failed"
    unknown = MountRecord(str(mountpoint), "disk.adf", "rw")
    with (
        patch("amigafs.desktop.mount_at", return_value=unknown),
        patch("amigafs.desktop.subprocess.run") as run,
        patch("amigafs.desktop._notify"),
        pytest.raises(AmigaFSError, match="no lifecycle identity record"),
    ):
        desktop_unmount(mountpoint)
    run.assert_not_called()


def test_read_only_desktop_unmount_can_detach_lazily(tmp_path: Path) -> None:
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    record = MountRecord(str(mountpoint), "disk.adf", "ro", read_write=False)
    result = SimpleNamespace(returncode=0, stderr="")
    with (
        patch("amigafs.desktop.mount_at", return_value=record),
        patch("amigafs.desktop.subprocess.run", return_value=result) as run,
        patch("amigafs.desktop.wait_for_mount_shutdown") as wait,
        patch("amigafs.desktop._notify"),
    ):
        assert desktop_unmount(mountpoint) == 0
    assert run.call_args.args[0] == ["fusermount3", "-u", "-z", str(mountpoint)]
    wait.assert_not_called()
