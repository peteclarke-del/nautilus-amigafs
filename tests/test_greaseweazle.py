from __future__ import annotations

import shutil
from io import StringIO
from pathlib import Path
from subprocess import TimeoutExpired
from types import SimpleNamespace
from typing import Any

import pytest

from amigafs import greaseweazle
from amigafs.core.image import ROOT_INODE, AmigaImage
from amigafs.errors import AmigaFSError
from amigafs.greaseweazle import (
    changed_cylinders,
    detected_command,
    detected_drives,
    greaseweazle_format,
    normalise_drive,
    physical_write_available,
    read_floppy,
    responsive_command,
    supports_physical_write,
    write_floppy,
)
from amigafs.recovery import pending_recovery, recover_image, salvage_workspace
from tests.image_fixture import create_floppy, create_hard_disc, gzip_image, tree

VERIFIED = "Writing c=0-79:h=0-1\nT0.0: Writing Track\nT0.1: Writing Track\nAll tracks verified\n"


class _Process:
    def __init__(self, output: str, returncode: int = 0) -> None:
        self.stdout = StringIO(output)
        self.returncode = returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        return None


class Drive:
    """A pretend Greaseweazle with one floppy in drive A."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, disk: Path | None = None) -> None:
        self.disk = disk
        self.commands: list[list[str]] = []
        self.written: dict[int, bytes] = {}
        self.fail_write = False
        self.skip_verify = False
        monkeypatch.setattr("amigafs.greaseweazle.shutil.which", lambda _name: "/usr/bin/gw")
        monkeypatch.setattr(
            "amigafs.greaseweazle.subprocess.run",
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0, stdout="Rate: 300.000 rpm", stderr=""
            ),
        )
        monkeypatch.setattr("amigafs.greaseweazle.subprocess.Popen", self.popen)

    def popen(self, arguments: list[str], **kwargs: Any) -> _Process:
        assert kwargs.get("shell") is not True
        assert kwargs["stdin"] is not None
        self.commands.append(arguments)
        action = arguments[1]
        target = Path(arguments[-1])
        if action == "read":
            assert self.disk is not None
            density = "hd" if "--format=amiga.amigados_hd" in arguments else "dd"
            size = self.disk.stat().st_size
            if (size == 1_802_240) != (density == "hd"):
                total = 3520 if density == "hd" else 1760
                return _Process(f"Found 0 sectors of {total} (0%)\n")
            shutil.copyfile(self.disk, target)
            total = size // 512
            return _Process(
                f"Reading c=0-79:h=0-1\nT0.0: AmigaDOS (11/11 sectors)\n"
                f"Found {total} sectors of {total} (100%)\n"
            )
        if action == "write":
            if self.fail_write:
                return _Process("T3.0: Writing Track\nCommand Failed: No Index\n", 1)
            data = target.read_bytes()
            cylinder = 2 * (22 if len(data) == 1_802_240 else 11) * 512
            selected = next((item for item in arguments if item.startswith("--tracks=c=")), None)
            wanted = (
                [int(number) for number in selected.removeprefix("--tracks=c=").split(",")]
                if selected
                else range(len(data) // cylinder)
            )
            for number in wanted:
                self.written[number] = data[number * cylinder : (number + 1) * cylinder]
            if self.disk is not None:
                current = bytearray(self.disk.read_bytes())
                for number, content in self.written.items():
                    current[number * cylinder : (number + 1) * cylinder] = content
                self.disk.write_bytes(bytes(current))
            return _Process("Writing c=0-79:h=0-1\nT0.0: ok\n" if self.skip_verify else VERIFIED)
        raise AssertionError(f"unexpected gw action: {arguments}")


def test_detection_short_circuits_unsupported_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def which(_name: str) -> str | None:
        nonlocal called
        called = True
        return "/usr/bin/gw"

    monkeypatch.setattr("amigafs.greaseweazle.shutil.which", which)

    assert detected_command("hard-disc.hdf") is None
    assert called is False
    assert supports_physical_write("Game.ADF")
    assert not supports_physical_write("disc.ssd")


def test_detection_requires_executable_and_responsive_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("amigafs.greaseweazle.shutil.which", lambda _name: None)
    assert responsive_command() is None

    monkeypatch.setattr("amigafs.greaseweazle.shutil.which", lambda _name: "/usr/bin/gw")
    for result in (
        SimpleNamespace(returncode=1, stdout="", stderr=""),
        # The host tools exit successfully even when no device is attached.
        SimpleNamespace(returncode=0, stdout="Host Tools: 1.23\nDevice:\n  Not found\n", stderr=""),
    ):
        monkeypatch.setattr("amigafs.greaseweazle.subprocess.run", lambda *_a, _r=result, **_k: _r)
        assert detected_command("disc.adf") is None

    monkeypatch.setattr(
        "amigafs.greaseweazle.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="Device:\n  Port: /dev/ttyACM0\n  Model: Greaseweazle V4",
            stderr="",
        ),
    )
    assert detected_command("disc.adf") == "/usr/bin/gw"

    def timed_out(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutExpired("gw info", 4)

    monkeypatch.setattr("amigafs.greaseweazle.subprocess.run", timed_out)
    assert detected_command("disc.adf") is None


def test_menu_availability_uses_immediate_udev_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serial_devices = tmp_path / "by-id"
    serial_devices.mkdir()
    device = tmp_path / "ttyACM0"
    device.touch(mode=0o600)
    (serial_devices / "usb-Keir_Fraser_Greaseweazle_GW123-if00").symlink_to(device)
    (serial_devices / "usb-Some_Other_Serial-if00").symlink_to(device)
    monkeypatch.setattr(greaseweazle, "SERIAL_DEVICE_DIRECTORY", serial_devices)
    monkeypatch.setattr("amigafs.greaseweazle.shutil.which", lambda _name: "/usr/bin/gw")
    monkeypatch.setattr(
        "amigafs.greaseweazle.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("menu detection must not start gw"),
    )
    image = create_floppy(tmp_path)

    assert physical_write_available(image) is True
    assert greaseweazle.floppy_drive_available() is True
    monkeypatch.setattr(greaseweazle, "SERIAL_DEVICE_DIRECTORY", tmp_path / "missing")
    assert physical_write_available(image) is False
    monkeypatch.setattr(greaseweazle, "SERIAL_DEVICE_DIRECTORY", serial_devices)
    monkeypatch.setattr("amigafs.greaseweazle.shutil.which", lambda _name: None)
    assert physical_write_available(image) is False


def test_only_plausible_amiga_floppy_images_are_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("amigafs.greaseweazle.floppy_drive_available", lambda: True)
    acorn = tmp_path / "acorn.adf"
    acorn.write_bytes(bytes(800 * 1024))
    blank = tmp_path / "blank.adf"
    blank.write_bytes(bytes(880 * 1024))
    fake_flux = tmp_path / "fake.hfe"
    fake_flux.write_bytes(b"not an hfe")
    flux = tmp_path / "real.hfe"
    flux.write_bytes(b"HXCHFEV3" + bytes(1024))
    hard_disc = create_hard_disc(tmp_path, capacity="4MB")

    assert physical_write_available(create_floppy(tmp_path)) is True
    assert physical_write_available(create_floppy(tmp_path, name="hd", density="hd")) is True
    assert physical_write_available(flux) is True
    assert physical_write_available(gzip_image(blank, tmp_path / "packed.adz")) is True
    for refused in (acorn, blank, fake_flux, hard_disc):
        assert physical_write_available(refused) is False


def test_sector_images_select_an_explicit_amiga_format(tmp_path: Path) -> None:
    assert greaseweazle_format(create_floppy(tmp_path)) == "amiga.amigados"
    assert (
        greaseweazle_format(create_floppy(tmp_path, name="hd", density="hd")) == "amiga.amigados_hd"
    )
    acorn = tmp_path / "acorn.adf"
    acorn.write_bytes(bytes(800 * 1024))
    with pytest.raises(AmigaFSError, match="neither an 880 KiB nor a 1760 KiB"):
        greaseweazle_format(acorn)
    with pytest.raises(AmigaFSError, match="Could not open"):
        greaseweazle_format(tmp_path / "missing.adf")


def test_drive_names_are_validated() -> None:
    assert normalise_drive("a") == "A"
    assert normalise_drive(" 2 ") == "2"
    for bad in ("C", "4", "", "A;reboot", "--drive=B"):
        with pytest.raises(AmigaFSError, match="invalid"):
            normalise_drive(bad)


def test_drive_detection_returns_only_pc_drives_with_index_pulses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = [
        SimpleNamespace(returncode=0, stdout="Command Failed: No Index\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="Rate: 299.981 rpm ; Period: 200.013 ms\n", stderr=""),
    ]
    monkeypatch.setattr("amigafs.greaseweazle.subprocess.run", lambda *_a, **_k: completed.pop(0))

    drives = detected_drives(command="/usr/bin/gw")

    assert drives == ("B",)
    assert completed == []


def test_drive_detection_falls_back_to_shugart_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = iter(["", "", "Rate: 300.000 rpm ; Period: 200.000 ms", "", "", ""])
    commands: list[list[str]] = []

    def probe(arguments: list[str], **_kwargs: Any) -> Any:
        commands.append(arguments)
        return SimpleNamespace(returncode=0, stdout=next(outputs), stderr="")

    monkeypatch.setattr("amigafs.greaseweazle.subprocess.run", probe)
    updates: list[int] = []
    drives = detected_drives(
        command="/usr/bin/gw", progress=lambda percent, _t: updates.append(percent)
    )

    assert drives == ("0",)
    assert [command[2] for command in commands] == [f"--drive={d}" for d in "AB0123"]
    assert all(command[:2] == ["/usr/bin/gw", "rpm"] for command in commands)
    assert updates == sorted(updates) and updates[-1] == 100


def test_drive_probe_timeout_resets_the_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def probe(arguments: list[str], **_kwargs: Any) -> Any:
        commands.append(arguments)
        if arguments[1] == "rpm":
            raise TimeoutExpired("gw rpm", 5)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("amigafs.greaseweazle.subprocess.run", probe)
    assert detected_drives(command="/usr/bin/gw") == ()
    assert [command[1] for command in commands].count("reset") == 6


def test_write_uses_a_stable_snapshot_an_explicit_format_and_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = create_floppy(tmp_path)
    drive = Drive(monkeypatch)
    updates: list[tuple[int, str]] = []

    result = write_floppy(
        image, "a", progress=lambda percent, text: updates.append((percent, text))
    )

    assert (result.drive, result.verified) == ("A", True)
    (command,) = drive.commands
    assert command[:4] == ["/usr/bin/gw", "write", "--drive=A", "--format=amiga.amigados"]
    snapshot = Path(command[-1])
    assert snapshot != image and snapshot.name == "image.adf"
    assert not snapshot.exists()
    assert b"".join(drive.written[number] for number in range(80)) == image.read_bytes()
    assert updates[0][0] == 1 and updates[-1] == (100, "All tracks written and verified.")


@pytest.mark.parametrize("container", ["adz", "dms"])
def test_compressed_images_are_decoded_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container: str
) -> None:
    source = create_floppy(tmp_path)
    if container == "adz":
        image = gzip_image(source, tmp_path / "disk.adz")
    else:
        image = tmp_path / "disk.dms"
        image.write_bytes(b"DMS!" + bytes(64))
        monkeypatch.setattr("amigafs._vendor.dms.to_adf", lambda _data: source.read_bytes())
    drive = Drive(monkeypatch)
    write_floppy(image, "B")
    (command,) = drive.commands
    assert "--format=amiga.amigados" in command
    assert command[-1].endswith(".adf")
    assert not Path(command[-1]).exists()
    assert b"".join(drive.written[number] for number in range(80)) == source.read_bytes()


def test_track_level_images_are_passed_through_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "protected.scp"
    image.write_bytes(b"SCP" + bytes(5000))
    commands: list[list[str]] = []
    captured: list[bytes] = []

    def popen(arguments: list[str], **_kwargs: Any) -> _Process:
        commands.append(arguments)
        captured.append(Path(arguments[-1]).read_bytes())
        return _Process("T0.0: Writing Track\n")

    monkeypatch.setattr("amigafs.greaseweazle.shutil.which", lambda _name: "/usr/bin/gw")
    monkeypatch.setattr(
        "amigafs.greaseweazle.subprocess.run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr("amigafs.greaseweazle.subprocess.Popen", popen)
    result = write_floppy(image, "A")
    # A flux image cannot be verified by reading it back, and is never re-encoded.
    assert result.verified is False
    assert not any(item.startswith("--format") for item in commands[0])
    assert commands[0][-1].endswith("image.scp")
    assert captured == [image.read_bytes()]


def test_failed_and_unverified_writes_warn_that_the_floppy_is_unreliable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = create_floppy(tmp_path)
    drive = Drive(monkeypatch)
    drive.fail_write = True
    with pytest.raises(AmigaFSError, match="No Index.*may be incomplete"):
        write_floppy(image, "A")
    drive.fail_write = False
    drive.skip_verify = True
    with pytest.raises(AmigaFSError, match="did not confirm verification"):
        write_floppy(image, "A")


def test_write_refuses_bad_input_before_touching_the_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive(monkeypatch)
    acorn = tmp_path / "acorn.adf"
    acorn.write_bytes(bytes(800 * 1024))
    document = tmp_path / "notes.txt"
    document.write_text("hello", encoding="utf-8")
    extended = tmp_path / "ext.adf"
    extended.write_bytes(b"UAE-1ADF" + bytes(100))
    with pytest.raises(AmigaFSError, match="neither an 880 KiB"):
        write_floppy(acorn, "A")
    with pytest.raises(AmigaFSError, match="does not write this kind of file"):
        write_floppy(document, "A")
    with pytest.raises(AmigaFSError, match="cannot be written to a physical floppy"):
        write_floppy(extended, "A")
    with pytest.raises(AmigaFSError, match="invalid"):
        write_floppy(create_floppy(tmp_path), "Z")
    with pytest.raises(AmigaFSError, match="Could not open"):
        write_floppy(tmp_path / "missing.adf", "A")
    assert drive.commands == []
    monkeypatch.setattr("amigafs.greaseweazle.shutil.which", lambda _name: None)
    with pytest.raises(AmigaFSError, match="No responsive Greaseweazle"):
        write_floppy(create_floppy(tmp_path, name="second"), "A")


def test_read_captures_a_complete_floppy_without_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = create_floppy(tmp_path / "drive" if (tmp_path / "drive").mkdir() is None else tmp_path)
    drive = Drive(monkeypatch, disk)
    out = tmp_path / "out"
    out.mkdir()
    result = read_floppy(out / "captured.adf", "A")
    assert result.path == out / "captured.adf"
    assert result.floppy_format.greaseweazle_name == "amiga.amigados"
    assert result.path.read_bytes() == disk.read_bytes()
    assert result.path.stat().st_mode & 0o777 == 0o644
    assert drive.commands[0][:4] == ["/usr/bin/gw", "read", "--drive=A", "--format=amiga.amigados"]
    assert sorted(child.name for child in out.iterdir()) == ["captured.adf"]
    with pytest.raises(AmigaFSError, match="Refusing to overwrite"):
        read_floppy(out / "CAPTURED.adf", "A")
    with pytest.raises(AmigaFSError, match="captured as an .adf"):
        read_floppy(out / "captured.img", "A")
    with pytest.raises(AmigaFSError, match="does not exist"):
        read_floppy(tmp_path / "nowhere" / "x.adf", "A")


def test_high_density_floppy_is_found_after_double_density_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = create_floppy(tmp_path, density="hd")
    drive = Drive(monkeypatch, disk)
    result = read_floppy(tmp_path / "hd-copy.adf", "A")
    assert result.floppy_format.greaseweazle_name == "amiga.amigados_hd"
    assert [command[3] for command in drive.commands] == [
        "--format=amiga.amigados",
        "--format=amiga.amigados_hd",
    ]
    drive.commands.clear()
    read_floppy(tmp_path / "hd-direct.adf", "A", density="hd")
    assert [command[3] for command in drive.commands] == ["--format=amiga.amigados_hd"]
    with pytest.raises(AmigaFSError, match="auto, dd or hd"):
        read_floppy(tmp_path / "x.adf", "A", density="ed")


def test_incomplete_read_is_never_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def popen(arguments: list[str], **_kwargs: Any) -> _Process:
        Path(arguments[-1]).write_bytes(bytes(901_120))
        return _Process("T40.1: AmigaDOS (9/11 sectors)\nFound 1758 sectors of 1760 (99%)\n")

    monkeypatch.setattr("amigafs.greaseweazle.shutil.which", lambda _name: "/usr/bin/gw")
    monkeypatch.setattr(
        "amigafs.greaseweazle.subprocess.run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr("amigafs.greaseweazle.subprocess.Popen", popen)
    with pytest.raises(AmigaFSError, match="could not be read as a complete AmigaDOS disk"):
        read_floppy(tmp_path / "damaged.adf", "A")
    assert list(tmp_path.iterdir()) == []


def test_changed_cylinders_are_found_exactly(tmp_path: Path) -> None:
    original = create_floppy(tmp_path)
    edited = tmp_path / "edited.adf"
    data = bytearray(original.read_bytes())
    cylinder = 2 * 11 * 512
    data[5 * cylinder + 3] ^= 1
    data[79 * cylinder + cylinder - 1] ^= 1
    edited.write_bytes(data)
    floppy_format = greaseweazle.floppy_format_for_size(901_120)
    assert floppy_format is not None
    assert changed_cylinders(original, edited, floppy_format) == (5, 79)
    assert changed_cylinders(original, original, floppy_format) == ()


def test_physical_floppy_mounts_read_only_without_writing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = create_floppy(tmp_path)
    before = disk.read_bytes()
    with AmigaImage.open(disk) as plain:
        expected = tree(plain)
    drive = Drive(monkeypatch, disk)
    with AmigaImage.open("floppy:A") as image:
        assert image.source.kind == "physical-floppy"
        assert tree(image) == expected
    assert [command[1] for command in drive.commands] == ["read"]
    assert disk.read_bytes() == before
    assert pending_recovery(image.source.primary_path) is None


def test_physical_floppy_writes_back_only_the_changed_cylinders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = create_floppy(tmp_path)
    drive = Drive(monkeypatch, disk)
    with AmigaImage.open("floppy:A", writable=True) as image:
        token = image.source.primary_path
        assert pending_recovery(token) is not None
        node = image.create_file(ROOT_INODE, b"OnTheDisk")
        image.replace_file(node.inode, b"written to a real floppy")
        assert [command[1] for command in drive.commands] == ["read"]
        assert image.needs_write_back
    write = drive.commands[-1]
    assert write[:4] == ["/usr/bin/gw", "write", "--drive=A", "--format=amiga.amigados"]
    tracks = next(item for item in write if item.startswith("--tracks=c="))
    cylinders = [int(number) for number in tracks.removeprefix("--tracks=c=").split(",")]
    assert cylinders == sorted(set(cylinders))
    assert 0 < len(cylinders) < 10
    assert sorted(drive.written) == cylinders
    assert pending_recovery(token) is None
    with AmigaImage.open(disk) as result:
        assert result.read(result.node_at_path("OnTheDisk").inode, 0, 99) == (
            b"written to a real floppy"
        )
        assert result.integrity_report().findings == ()


def test_unmodified_writable_floppy_session_never_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive(monkeypatch, create_floppy(tmp_path))
    with AmigaImage.open("floppy:A", writable=True) as image:
        assert not image.needs_write_back
    assert [command[1] for command in drive.commands] == ["read"]


def test_failed_write_back_keeps_the_changes_for_salvage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = create_floppy(tmp_path)
    before = disk.read_bytes()
    drive = Drive(monkeypatch, disk)
    image = AmigaImage.open("floppy:A", writable=True)
    token = image.source.primary_path
    node = image.create_file(ROOT_INODE, b"Precious")
    image.replace_file(node.inode, b"must not be lost")
    drive.fail_write = True
    with pytest.raises(AmigaFSError, match="amigafs recover floppy:A --salvage"):
        image.close()
    assert disk.read_bytes() == before
    info = pending_recovery(token)
    assert info is not None and info.kind == "workspace" and info.detail == "greaseweazle"
    with pytest.raises(AmigaFSError, match="needs recovery"):
        AmigaImage.open("floppy:A", writable=True)
    saved = salvage_workspace(token, tmp_path / "rescued.adf")
    with AmigaImage.open(saved) as rescued:
        assert rescued.read(rescued.node_at_path("Precious").inode, 0, 99) == b"must not be lost"
    recover_image(token, discard=True)
    assert pending_recovery(token) is None


def test_one_drive_cannot_be_mounted_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    Drive(monkeypatch, create_floppy(tmp_path))
    with (
        AmigaImage.open("floppy:A", writable=True),
        pytest.raises(AmigaFSError, match="another AmigaFS process"),
    ):
        AmigaImage.open("floppy:A")


def test_unreadable_floppy_leaves_no_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("amigafs.greaseweazle.shutil.which", lambda _name: "/usr/bin/gw")
    monkeypatch.setattr(
        "amigafs.greaseweazle.subprocess.run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        "amigafs.greaseweazle.subprocess.Popen",
        lambda *_a, **_k: _Process("Command Failed: No Index\n", 1),
    )
    with pytest.raises(AmigaFSError, match="could not be read as a complete AmigaDOS disk"):
        AmigaImage.open("floppy:A", writable=True)
    from amigafs.core.formats import resolve_image

    assert pending_recovery(resolve_image("floppy:A").primary_path) is None
    assert not list((tmp_path / "state").rglob("*.adf"))
