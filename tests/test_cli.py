from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from amigafs.cli import main
from amigafs.core.image import ROOT_INODE, AmigaImage
from amigafs.errors import AmigaFSError
from amigafs.mounts import MountRecord
from amigafs.recovery import pending_recovery
from tests.image_fixture import create_floppy, create_hard_disc, gzip_image, invalidate_bitmap


def test_inspect_error_is_concise(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = main(["inspect", str(tmp_path / "missing.adf")])
    assert result == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("amigafs: ")
    assert "does not exist" in captured.err
    assert "Traceback" not in captured.err


def test_inspect_describes_partitions_and_volumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image_path = create_hard_disc(tmp_path, filesystem="SFS", capacity="8MB", partitions=2)
    assert main(["inspect", str(image_path)]) == 0
    output = capsys.readouterr().out
    assert "Type: Amiga hard-disc image" in output
    assert "Layout: Rigid Disk Block partition table" in output
    assert "heads, 63 sectors per track" in output
    assert "- DH0: SFS SFS\\0 'System'" in output
    assert "- DH1: SFS SFS\\0 'System1'" in output
    assert "bootable at priority 0" in output
    assert "Validation: passed" in output
    assert main(["inspect", "--json", str(image_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [volume["name"] for volume in payload["volumes"]] == ["DH0", "DH1"]


def test_create_commands_create_validated_images(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "create-floppy",
                str(tmp_path),
                "--name",
                "new-disk",
                "--title",
                "NewDisk",
                "--filesystem",
                "ffs-intl",
                "--density",
                "hd",
                "--bootable",
            ]
        )
        == 0
    )
    assert (tmp_path / "new-disk.adf").stat().st_size == 1_802_240
    output = capsys.readouterr().out
    assert "Created and verified floppy image" in output
    assert "Volume: NewDisk; FFS-INTL" in output
    assert (
        main(
            [
                "create-hard-disc",
                str(tmp_path),
                "--name",
                "new-drive",
                "--capacity",
                "16MB",
                "--filesystem",
                "pfs3",
                "--partitions",
                "2",
            ]
        )
        == 0
    )
    assert "Partitions: DH0, DH1; PFS3" in capsys.readouterr().out
    assert main(["create-floppy", str(tmp_path), "--name", "new-disk"]) == 2
    assert "would overwrite" in capsys.readouterr().err


def test_metadata_aware_export_and_import_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    images = tmp_path / "images"
    images.mkdir()
    image_path = create_floppy(images)
    exported = tmp_path / "ReadMe"

    assert main(["export-file", str(image_path), "Docs/ReadMe", str(exported)]) == 0
    assert exported.read_bytes() == b"Read me first.\n" * 40
    assert exported.with_name("ReadMe.inf").is_file()
    assert "Amiga metadata:" in capsys.readouterr().out

    assert (
        main(["import-file", str(image_path), str(exported), "--directory", "C", "--name", "Copy"])
        == 0
    )
    output = capsys.readouterr().out
    assert "as Workbench:C/Copy" in output
    assert "Metadata source: INF sidecar ReadMe.inf" in output
    with AmigaImage.open(image_path) as image:
        copied = image.node_at_path("C/Copy")
        assert image.read(copied.inode, 0, copied.size) == b"Read me first.\n" * 40
        assert copied.comment == "Introduction"


def test_status_reports_no_mounts(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("amigafs.cli.active_mounts", return_value=[]):
        assert main(["status"]) == 0
    assert capsys.readouterr().out == "No AmigaFS mounts found.\n"


def test_status_lists_and_filters_mounts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    records = [
        MountRecord(str(tmp_path / "one"), "one.adf", "ro"),
        MountRecord(str(tmp_path / "two"), "two.hdf", "rw"),
    ]
    with patch("amigafs.cli.active_mounts", return_value=records):
        assert main(["status"]) == 0
        assert capsys.readouterr().out.count(" on ") == 2
        assert main(["status", "--json", str(tmp_path / "two")]) == 0
    (listed,) = json.loads(capsys.readouterr().out)
    assert listed["source"] == "two.hdf"


def test_diagnostics_are_exportable(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["diagnostics", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["privacy"] == "No image contents or absolute paths are included."
    assert set(payload["hardware"]) == {"greaseweazle_tools", "device_helper", "polkit"}
    assert main(["diagnostics"]) == 0
    text = capsys.readouterr().out
    assert "Greaseweazle tools:" in text and "device helper:" in text


def test_list_discs_reports_eligible_and_refused_discs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from amigafs.core.device_policy import PhysicalDisc

    discs = [
        PhysicalDisc(
            name="sdb",
            device="/dev/sdb",
            stable_path="/dev/disk/by-id/usb-CF",
            model="CF Card",
            size=4_000_000_000,
            removable=True,
            usb=True,
            read_only=False,
        ),
        PhysicalDisc(
            name="sdc",
            device="/dev/sdc",
            stable_path="/dev/sdc",
            model="Stick",
            size=1_000_000,
            removable=True,
            usb=True,
            read_only=False,
            refusal="Part of this disc is mounted by Linux: /media/x",
            refusal_code="mounted",
        ),
    ]
    with patch("amigafs.core.devices.list_discs", return_value=discs) as listed:
        assert main(["list-discs", "--all"]) == 0
    listed.assert_called_once_with(include_refused=True)
    output = capsys.readouterr().out
    assert "/dev/disk/by-id/usb-CF" in output
    assert "CF Card, 3.7 GiB, /dev/sdb (needs authorisation)" in output
    assert "refused: Part of this disc is mounted" in output
    with patch("amigafs.core.devices.list_discs", return_value=[]):
        assert main(["list-discs"]) == 0
    assert "No removable or USB disc" in capsys.readouterr().out


def test_config_mount_location_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("AMIGAFS_MOUNT_ROOT", raising=False)

    assert main(["config-mount-location", "runtime"]) == 0
    output = capsys.readouterr().out
    assert str(runtime / "amigafs" / "images") in output
    assert "Mode: runtime; source: user" in output
    assert main(["config-mount-location"]) == 0
    assert "Mode: runtime; source: user" in capsys.readouterr().out
    assert main(["config-mount-location", "--reset"]) == 0
    assert "Mode: sidebar; source: default" in capsys.readouterr().out
    assert main(["config-mount-location", "runtime", "--reset"]) == 2


def test_lazy_unmount_is_refused_for_writable_image(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    record = MountRecord(str(mountpoint), "disk.adf", "rw", read_write=True)
    with (
        patch("amigafs.cli.mount_at", return_value=record),
        patch("amigafs.cli.subprocess.run") as run,
    ):
        assert main(["unmount", "--lazy", str(mountpoint)]) == 2
    run.assert_not_called()
    assert "read-only" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("kind", "timeout"),
    [("floppy-image", 120.0), ("compressed-image", 600.0), ("physical-floppy", 2100.0)],
)
def test_writable_unmount_waits_as_long_as_write_back_may_take(
    tmp_path: Path, kind: str, timeout: float
) -> None:
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    record = MountRecord(
        str(mountpoint),
        "disk.adf",
        "rw",
        image_path=str(tmp_path / "disk.adf"),
        image_kind=kind,
        read_write=True,
    )
    result = SimpleNamespace(returncode=0, stderr="")
    with (
        patch("amigafs.cli.mount_at", return_value=record),
        patch("amigafs.cli.subprocess.run", return_value=result) as run,
        patch("amigafs.cli.wait_for_mount_shutdown", return_value=True) as wait,
        patch("amigafs.cli.pending_recovery", return_value=None),
    ):
        assert main(["unmount", str(mountpoint)]) == 0
    run.assert_called_once()
    assert run.call_args.args[0] == ["fusermount3", "-u", str(mountpoint)]
    wait.assert_called_once_with(mountpoint, timeout=timeout)


def test_unmount_reports_an_unconfirmed_or_unfinalised_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    record = MountRecord(
        str(mountpoint), "disk.adf", "rw", image_path=str(tmp_path / "disk.adf"), read_write=True
    )
    result = SimpleNamespace(returncode=0, stderr="")
    with (
        patch("amigafs.cli.mount_at", return_value=record),
        patch("amigafs.cli.subprocess.run", return_value=result),
        patch("amigafs.cli.wait_for_mount_shutdown", return_value=False),
    ):
        assert main(["unmount", str(mountpoint)]) == 2
    assert "did not confirm final flush" in capsys.readouterr().err
    with (
        patch("amigafs.cli.mount_at", return_value=record),
        patch("amigafs.cli.subprocess.run", return_value=result),
        patch("amigafs.cli.wait_for_mount_shutdown", return_value=True),
        patch("amigafs.cli.pending_recovery", return_value=object()),
    ):
        assert main(["unmount", str(mountpoint)]) == 2
    assert "recovery checkpoint is pending" in capsys.readouterr().err
    with patch("amigafs.cli.mount_at", return_value=None):
        assert main(["unmount", str(mountpoint)]) == 2
    assert "No active AmigaFS mount" in capsys.readouterr().err


def test_recover_command_reports_restores_and_salvages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image_path = create_floppy(tmp_path)
    original = image_path.read_bytes()
    assert main(["recover", str(image_path)]) == 0
    assert "No recovery checkpoint is pending." in capsys.readouterr().out
    image = AmigaImage.open(image_path, writable=True)
    image.create_file(ROOT_INODE, b"Interrupted")
    image.store.handle.close()
    assert main(["recover", str(image_path)]) == 0
    assert "--restore" in capsys.readouterr().out
    assert main(["recover", str(image_path), "--salvage", str(tmp_path / "x.adf")]) == 2
    assert "No interrupted working copy" in capsys.readouterr().err
    assert main(["recover", "--restore", str(image_path)]) == 0
    assert "Recovery checkpoint restored." in capsys.readouterr().out
    assert image_path.read_bytes() == original

    packed = gzip_image(image_path, tmp_path / "disk.adz")
    session = AmigaImage.open(packed, writable=True)
    session.create_file(ROOT_INODE, b"Unsaved")
    session.close(clean=False)
    assert main(["recover", str(packed), "--salvage", str(tmp_path / "saved.adf")]) == 0
    assert "Saved the interrupted working copy" in capsys.readouterr().out
    assert main(["recover", str(packed), "--discard"]) == 0
    assert pending_recovery(packed) is None
    with AmigaImage.open(tmp_path / "saved.adf") as saved:
        assert saved.node_at_path("Unsaved").size == 0


def test_floppy_recovery_is_addressed_by_its_drive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch("amigafs.cli.recover_image", return_value="done") as recover:
        assert main(["recover", "floppy:a", "--discard"]) == 0
    reference = recover.call_args.args[0]
    assert Path(reference).name == "drive-A"
    assert recover.call_args.kwargs == {"restore": False, "discard": True}


def test_mount_command_forwards_its_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AMIGAFS_DESKTOP_MOUNT", raising=False)
    with patch("amigafs.fuse_adapter.runner.mount_image") as mount:
        assert main(["mount", "--read-write", "floppy:A", str(tmp_path)]) == 0
    assert mount.call_args.args == ("floppy:A", str(tmp_path))
    assert mount.call_args.kwargs["read_write"] is True
    assert callable(mount.call_args.kwargs["progress"])
    mount.call_args.kwargs["progress"](40, "Reading track 3.0")
    mount.call_args.kwargs["write_back_started"]("Floppy drive A")
    captured = capsys.readouterr()
    assert "[ 40%] Reading track 3.0" in captured.err
    assert "do not remove it" in captured.err
    assert "press Ctrl-C to stop" in captured.out


def test_detached_mount_output_does_not_log_image_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMIGAFS_DESKTOP_MOUNT", "1")
    image_path = tmp_path / "private" / "secret.adf"
    with patch("amigafs.fuse_adapter.runner.mount_image") as mount:
        assert main(["mount", str(image_path), str(tmp_path / "mount")]) == 0
    assert mount.call_args.kwargs["progress"] is None
    captured = capsys.readouterr()
    assert "secret.adf" not in captured.out + captured.err
    assert str(tmp_path) not in captured.out + captured.err


def test_detached_mount_error_redacts_unrelated_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMIGAFS_DESKTOP_MOUNT", "1")
    failure = AmigaFSError("could not read /home/alice/Private/other.adf")
    with (
        patch("amigafs.fuse_adapter.runner.mount_image", side_effect=failure),
        patch("amigafs.desktop.notify_mount_failure") as notify,
    ):
        assert main(["mount", str(tmp_path / "disk.adf"), str(tmp_path)]) == 2
    notify.assert_called_once_with("could not read <path>")
    assert "/home/alice" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "function", "expected_args", "expected_kwargs"),
    [
        (
            ["desktop-mount", "--read-write", "a.adf"],
            "desktop_mount",
            ("a.adf",),
            {"read_write": True},
        ),
        (["desktop-unmount", "/mnt/a"], "desktop_unmount", ("/mnt/a",), {}),
        (["desktop-recover", "a.adf"], "desktop_recover", ("a.adf",), {}),
        (["desktop-repair", "a.adf"], "desktop_repair", ("a.adf",), {}),
        (["desktop-validate", "a.adf"], "desktop_validate", ("a.adf",), {}),
        (["desktop-open-file-forge", "a.adf"], "desktop_open_file_forge", ("a.adf",), {}),
        (["desktop-write-floppy", "a.adf"], "desktop_write_floppy", ("a.adf",), {}),
        (["desktop-write-disc", "a.hdf"], "desktop_write_disc", ("a.hdf",), {}),
        (["desktop-mount-floppy"], "desktop_mount_floppy", (), {"read_write": False}),
        (
            ["desktop-mount-floppy", "--read-write"],
            "desktop_mount_floppy",
            (),
            {"read_write": True},
        ),
        (["desktop-mount-disc", "--read-write"], "desktop_mount_disc", (), {"read_write": True}),
        (["desktop-read-floppy", "/tmp"], "desktop_read_floppy", ("/tmp",), {}),
        (["desktop-read-disc", "/tmp"], "desktop_read_disc", ("/tmp",), {}),
        (
            ["desktop-open", "file:///a.adf", "b.hdf"],
            "desktop_open",
            (["file:///a.adf", "b.hdf"],),
            {"handed_off": False},
        ),
        (
            ["desktop-open", "--handed-off", "a.adf"],
            "desktop_open",
            (["a.adf"],),
            {"handed_off": True},
        ),
        (["desktop-claims", "a.adf"], "desktop_claims", ("a.adf",), {}),
        (["desktop-create", "/tmp"], "desktop_create", ("/tmp",), {"kind": "floppy"}),
        (
            ["desktop-create", "--kind", "hard-disc", "/tmp"],
            "desktop_create",
            ("/tmp",),
            {"kind": "hard-disc"},
        ),
        (["desktop-configure-mount-location"], "desktop_configure_mount_location", (), {}),
    ],
)
def test_desktop_commands_forward_to_the_desktop_layer(
    arguments: list[str],
    function: str,
    expected_args: tuple[object, ...],
    expected_kwargs: dict[str, object],
) -> None:
    with patch(f"amigafs.desktop.{function}", return_value=0) as target:
        assert main(arguments) == 0
    target.assert_called_once_with(*expected_args, **expected_kwargs)


def test_physical_transfer_commands_forward_and_require_confirmation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from amigafs.core.containers import FLOPPY_FORMATS
    from amigafs.core.disc_transfer import DiscTransferResult
    from amigafs.greaseweazle import FloppyReadResult, FloppyWriteResult

    image = tmp_path / "disk.adf"
    assert main(["write-floppy", str(image), "A"]) == 2
    assert "--yes to confirm" in capsys.readouterr().err
    with patch(
        "amigafs.greaseweazle.write_floppy", return_value=FloppyWriteResult("A", True, 160)
    ) as write:
        assert main(["write-floppy", "--yes", str(image), "a"]) == 0
    assert write.call_args.args == (str(image), "a")
    assert "to drive A and verified" in capsys.readouterr().out
    captured = FloppyReadResult("B", tmp_path / "out.adf", FLOPPY_FORMATS[1])
    with patch("amigafs.greaseweazle.read_floppy", return_value=captured) as read:
        assert main(["read-floppy", "B", str(tmp_path / "out.adf"), "--density", "dd"]) == 0
    assert read.call_args.kwargs["density"] == "dd"
    assert "Amiga DD, 880 KiB" in capsys.readouterr().out
    result = DiscTransferResult("/dev/sdb", tmp_path / "card.hdf", 1024, "ab" * 32)
    with patch("amigafs.core.disc_transfer.read_disc", return_value=result) as read_disc:
        assert main(["read-disc", "/dev/sdb", str(tmp_path / "card.hdf")]) == 0
    assert read_disc.call_args.args == ("/dev/sdb", str(tmp_path / "card.hdf"))
    assert "SHA-256: " + "ab" * 32 in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["write-disc", str(tmp_path / "card.hdf"), "/dev/sdb"])
    with patch("amigafs.core.disc_transfer.write_disc", return_value=result) as write_disc:
        assert main(["write-disc", str(tmp_path / "card.hdf"), "/dev/sdb", "--confirm", "sdb"]) == 0
    assert write_disc.call_args.kwargs["confirmation"] == "sdb"


def test_validate_command_reports_clean_and_damaged_images(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image_path = create_floppy(tmp_path)
    assert main(["validate", str(image_path)]) == 0
    assert "passed with no problems" in capsys.readouterr().out
    invalidate_bitmap(image_path)
    assert main(["validate", str(image_path)]) == 1
    assert "[FATAL] bitmap.inconsistent" in capsys.readouterr().out
    assert main(["validate", "--json", str(image_path)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["safe_for_write"] is False
    assert payload["volumes"][0]["title"] == "Workbench"
    assert main(["validate", str(tmp_path / "missing.adf")]) == 2


def test_repair_plan_json_is_dry_run_and_does_not_modify_image(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image_path = create_floppy(tmp_path)
    invalidate_bitmap(image_path)
    before = image_path.read_bytes()
    assert main(["repair-plan", "--json", str(image_path)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["application_supported"] is True
    assert image_path.read_bytes() == before
    assert main(["repair-plan", str(image_path)]) == 1
    assert "--confirm IMAGE_FILENAME" in capsys.readouterr().out


def test_repair_command_applies_eligible_plan_and_reports_audit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image_path = create_floppy(tmp_path)
    good = image_path.read_bytes()
    invalidate_bitmap(image_path)
    assert main(["repair", str(image_path), "--confirm", "wrong"]) == 2
    assert "must exactly match" in capsys.readouterr().err
    assert main(["repair", str(image_path), "--confirm", image_path.name]) == 0
    output = capsys.readouterr().out
    assert "Applied 1 repair action(s)." in output
    assert "Audit report: " in output
    assert image_path.read_bytes() == good
    assert main(["repair", "--json", str(image_path), "--confirm", image_path.name]) == 2
