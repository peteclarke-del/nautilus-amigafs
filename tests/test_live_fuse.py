from __future__ import annotations

import errno
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from amigafs.core import AmigaImage, validate_image_report
from amigafs.desktop import _systemd_user_available, background_mount, desktop_unmount
from amigafs.fuse_adapter.availability import live_fuse_available
from amigafs.mounts import is_mounted, mount_for_image
from amigafs.recovery import pending_recovery, recover_image
from tests.image_fixture import create_floppy, create_hard_disc, gzip_image

REAL_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR")
FUSE_REQUESTED = os.environ.get("AMIGAFS_RUN_LIVE_FUSE") == "1"
FUSE_AVAILABLE = FUSE_REQUESTED and live_fuse_available()
SYSTEMD_AVAILABLE = FUSE_AVAILABLE and _systemd_user_available()
FUSE_SKIP_REASON = (
    "a usable /dev/fuse device and fusermount3 are required"
    if FUSE_REQUESTED
    else "set AMIGAFS_RUN_LIVE_FUSE=1 on a host permitted to mount FUSE filesystems"
)
live = pytest.mark.skipif(not FUSE_AVAILABLE, reason=FUSE_SKIP_REASON)


def _run(*command: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command), cwd=cwd, check=check, capture_output=True, text=True, timeout=30
    )


def _start(image: Path | str, mountpoint: Path, *, read_write: bool) -> subprocess.Popen[str]:
    mountpoint.mkdir(exist_ok=True)
    command = [sys.executable, "-m", "amigafs.cli", "mount"]
    if read_write:
        command.append("--read-write")
    command.extend((str(image), str(mountpoint)))
    process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )
    deadline = time.monotonic() + 30
    while mount_for_image(image) is None:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            pytest.fail(f"FUSE mount exited early: {stderr}")
        if time.monotonic() >= deadline:
            process.kill()
            pytest.fail("FUSE mount did not become ready within 30 seconds")
        time.sleep(0.05)
    return process


def _force_detach(process: subprocess.Popen[str], mountpoint: Path) -> None:
    if process.poll() is None:
        subprocess.run(
            ["fusermount3", "-uz", str(mountpoint)], check=False, capture_output=True, timeout=15
        )
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
    elif is_mounted(mountpoint):
        subprocess.run(
            ["fusermount3", "-uz", str(mountpoint)], check=False, capture_output=True, timeout=15
        )


@contextmanager
def mounted(image: Path | str, mountpoint: Path, *, read_write: bool = False) -> Iterator[Path]:
    """Mount through the real kernel, then unmount and require a clean daemon exit."""

    process = _start(image, mountpoint, read_write=read_write)
    try:
        yield mountpoint
        unmount = subprocess.run(
            ["fusermount3", "-u", str(mountpoint)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert unmount.returncode == 0, unmount.stderr
        stderr = process.communicate(timeout=60)[1]
        assert process.returncode == 0, stderr
    finally:
        _force_detach(process, mountpoint)


@live
@pytest.mark.parametrize("filesystem", ["OFS", "FFS-INTL", "FFS-DC", "FFS-LNFS"])
def test_live_read_only_floppy_mount(tmp_path: Path, filesystem: str) -> None:
    """Traverse a floppy image with ordinary terminal tools."""

    image = create_floppy(tmp_path, filesystem=filesystem)
    original = image.read_bytes()
    with mounted(image, tmp_path / "mount") as mountpoint:
        listing = _run("find", ".", "-print", cwd=mountpoint)
        assert sorted(listing.stdout.splitlines()) == [
            ".",
            "./C",
            "./C/List",
            "./Docs",
            "./Docs/Deep",
            "./Docs/Deep/Nested",
            "./Docs/Deep/Nested/Note",
            "./Docs/ReadMe",
            "./Empty",
            "./S",
            "./S/Startup-Sequence",
        ]
        contents = subprocess.run(
            ["cat", "S/Startup-Sequence", "Docs/Deep/Nested/Note"],
            cwd=mountpoint,
            check=True,
            capture_output=True,
            timeout=15,
        )
        assert contents.stdout == b"; boot\nLoadWB\nEndCLI\nnested"
        # Names resolve without regard to case, as they do on an Amiga.
        assert (mountpoint / "docs" / "README").read_bytes() == b"Read me first.\n" * 40
        modes = _run("stat", "--format=%a", "Docs/ReadMe", "Docs", cwd=mountpoint)
        assert modes.stdout.splitlines() == ["444", "555"]
        refused = _run("touch", "New", cwd=mountpoint, check=False)
        assert refused.returncode != 0
        assert os.getxattr(mountpoint / "Docs/ReadMe", "user.amiga.comment") == b"Introduction"
        assert os.getxattr(mountpoint / "S/Startup-Sequence", "user.amiga.protection") == (
            b"-s--rwed"
        )
        assert os.getxattr(mountpoint / "C/List", "user.amiga.source") == filesystem.encode()
        assert os.getxattr(mountpoint / "C/List", "user.amiga.path") == b"Workbench:C/List"
        assert os.statvfs(mountpoint).f_namemax == (107 if filesystem == "FFS-LNFS" else 30)
    assert image.read_bytes() == original
    assert pending_recovery(image) is None


@live
@pytest.mark.parametrize("filesystem", ["FFS", "PFS3", "SFS"])
def test_live_read_only_hard_disc_mount_exposes_every_partition(
    tmp_path: Path, filesystem: str
) -> None:
    image = create_hard_disc(tmp_path, filesystem=filesystem, capacity="8MB", partitions=2)
    original = image.read_bytes()
    with mounted(image, tmp_path / "mount") as mountpoint:
        assert sorted(child.name for child in mountpoint.iterdir()) == ["DH0", "DH1"]
        for partition in ("DH0", "DH1"):
            assert (mountpoint / partition / "C" / "List").read_bytes() == bytes(range(256)) * 12
            assert (mountpoint / partition / "Docs/Deep/Nested/Note").read_bytes() == b"nested"
        assert os.getxattr(mountpoint / "DH1", "user.amiga.volume") == b"System1"
        assert os.getxattr(mountpoint / "DH1/C/List", "user.amiga.path") == b"DH1:C/List"
    assert image.read_bytes() == original


@live
def test_live_compressed_image_round_trips_through_the_kernel(tmp_path: Path) -> None:
    image = gzip_image(create_floppy(tmp_path), tmp_path / "disk.adz")
    with mounted(image, tmp_path / "mount", read_write=True) as mountpoint:
        (mountpoint / "Added").write_bytes(b"through gzip and FUSE")
    assert pending_recovery(image) is None
    with AmigaImage.open(image) as reopened:
        assert reopened.read(reopened.node_at_path("Added").inode, 0, 99) == (
            b"through gzip and FUSE"
        )
    assert validate_image_report(image).findings == ()


@live
@pytest.mark.parametrize("filesystem", ["OFS", "FFS-DC", "FFS-LNFS", "PFS3", "SFS"])
def test_live_writable_lifecycle(tmp_path: Path, filesystem: str) -> None:
    """Use cp, mv, rm, mkdir, truncate, chmod, touch and setfattr on a writable mount."""

    if filesystem in {"PFS3", "SFS"}:
        image = create_hard_disc(tmp_path, filesystem=filesystem, capacity="8MB", partitions=1)
        top = Path("DH0")
        prefix = "DH0:"
    else:
        image = create_floppy(tmp_path, filesystem=filesystem)
        top = Path()
        prefix = ""
    host = tmp_path / "host-file"
    payload = bytes(range(256)) * 300
    host.write_bytes(payload)
    with mounted(image, tmp_path / "mount", read_write=True) as mountpoint:
        root = mountpoint / top
        assert pending_recovery(image) is not None
        _run("mkdir", "Projects", cwd=root)
        _run("cp", str(host), "Projects/CopiedIn", cwd=root)
        assert (root / "Projects/CopiedIn").read_bytes() == payload
        _run("mv", "Projects/CopiedIn", "Renamed", cwd=root)
        _run("truncate", "--size=1000", "Renamed", cwd=root)
        assert (root / "Renamed").read_bytes() == payload[:1000]
        with (root / "Renamed").open("ab") as handle:
            handle.write(b"appended")
        assert (root / "Renamed").read_bytes() == payload[:1000] + b"appended"
        # An editor-style save: write a temporary file, then rename it over the original.
        (root / "Docs/ReadMe.tmp").write_bytes(b"saved by an editor")
        os.replace(root / "Docs/ReadMe.tmp", root / "Docs/ReadMe")
        assert (root / "Docs/ReadMe").read_bytes() == b"saved by an editor"
        os.setxattr(root / "Renamed", "user.amiga.comment", "Größe".encode())
        os.setxattr(root / "Renamed", "user.amiga.protection", b"-s--rwed")
        os.utime(root / "Renamed", ns=(600_000_000 * 10**9, 600_000_000 * 10**9))
        assert (root / "Renamed").stat().st_mtime_ns == 600_000_000 * 10**9
        os.chmod(root / "C/List", 0o444)
        assert (root / "C/List").stat().st_mode & 0o777 == 0o444
        refused = _run("rm", "-f", "C/List", cwd=root, check=False)
        assert refused.returncode != 0
        os.chmod(root / "C/List", 0o644)
        _run("rm", "C/List", "Empty", cwd=root)
        _run("rmdir", "Projects", cwd=root)
        not_empty = _run("rmdir", "Docs", cwd=root, check=False)
        assert not_empty.returncode != 0
        too_long = _run("touch", "n" * 120, cwd=root, check=False)
        assert too_long.returncode != 0
        illegal = _run("touch", "a:b", cwd=root, check=False)
        assert illegal.returncode != 0
        too_big = subprocess.run(
            ["dd", "if=/dev/zero", "of=Huge", "bs=1M", "count=16"],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=60,
        )
        assert too_big.returncode != 0
        (root / "Huge").unlink(missing_ok=True)
        _run("sync", cwd=root)
    assert pending_recovery(image) is None
    assert validate_image_report(image).findings == ()
    with AmigaImage.open(image) as reopened:
        renamed = reopened.node_at_path(f"{prefix}Renamed")
        assert reopened.read(renamed.inode, 0, renamed.size) == payload[:1000] + b"appended"
        assert (renamed.comment, renamed.protection) == ("Größe", 0x40)
        assert renamed.mtime_ns == 600_000_000 * 10**9
        names = {node.amiga_path for node in reopened.nodes.values()}
        assert not any(name.endswith(("Projects", "C/List", ":Empty", "Huge")) for name in names)
        readme = reopened.node_at_path(f"{prefix}Docs/ReadMe")
        assert reopened.read(readme.inode, 0, 99) == b"saved by an editor"


@live
def test_live_large_file_is_read_in_ranges(tmp_path: Path) -> None:
    payload = os.urandom(3 * 1024 * 1024)
    image = create_hard_disc(
        tmp_path, capacity="16MB", partitions=1, files=(("Big", payload, 0, ""),)
    )
    with mounted(image, tmp_path / "mount") as mountpoint:
        with (mountpoint / "DH0/Big").open("rb") as handle:
            handle.seek(2_000_000)
            assert handle.read(4096) == payload[2_000_000:2_004_096]
        copied = tmp_path / "copied"
        shutil.copyfile(mountpoint / "DH0/Big", copied)
        assert copied.read_bytes() == payload


@live
@pytest.mark.skipif(not SYSTEMD_AVAILABLE, reason="a systemd user manager is required")
def test_live_systemd_writable_mount_is_recognised_and_finalised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the exact transient-service path used by Nautilus."""

    assert REAL_RUNTIME_DIR is not None
    # The user manager lives in the real runtime directory.
    monkeypatch.setenv("XDG_RUNTIME_DIR", REAL_RUNTIME_DIR)
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    image = create_floppy(image_dir)
    mount_root = tmp_path / "mounts"
    monkeypatch.setenv("AMIGAFS_MOUNT_ROOT", str(mount_root))
    monkeypatch.setattr("amigafs.desktop._notify", lambda *_args, **_kwargs: None)
    mountpoint = background_mount(
        image, open_folder=False, notify=False, timeout=30, read_write=True
    )
    try:
        record = mount_for_image(image)
        assert record is not None
        assert record.read_write is True
        assert record.image_kind == "floppy-image"
        (mountpoint / "Systemd").write_bytes(b"managed writable mount")
        assert desktop_unmount(mountpoint) == 0
    finally:
        if is_mounted(mountpoint):
            subprocess.run(
                ["fusermount3", "-uz", str(mountpoint)],
                check=False,
                capture_output=True,
                timeout=15,
            )

    assert validate_image_report(image).findings == ()
    assert pending_recovery(image) is None
    with AmigaImage.open(image) as reopened:
        assert reopened.node_at_path("Systemd").size == 22


@live
def test_live_forced_daemon_termination_restores_checkpoint(tmp_path: Path) -> None:
    """Prove a killed writable daemon leaves a restorable pre-mount checkpoint."""

    image = create_hard_disc(tmp_path, filesystem="PFS3", capacity="8MB", partitions=1)
    original = image.read_bytes()
    mountpoint = tmp_path / "crash-mount"
    process = _start(image, mountpoint, read_write=True)
    try:
        (mountpoint / "DH0" / "Crashed").write_bytes(b"must be rolled back" * 1000)
        (mountpoint / "DH0" / "AlsoLost").mkdir()
        process.kill()
        process.wait(timeout=15)
    finally:
        _force_detach(process, mountpoint)

    assert image.read_bytes() != original
    assert pending_recovery(image) is not None
    assert "restored" in recover_image(image, restore=True)
    assert image.read_bytes() == original
    assert validate_image_report(image).findings == ()
    assert pending_recovery(image) is None


@live
def test_live_sigint_flushes_a_dirty_open_handle(tmp_path: Path) -> None:
    """Model graceful systemd logout while an application still has dirty data open."""

    image = create_floppy(tmp_path)
    mountpoint = tmp_path / "dirty-shutdown-mount"
    process = _start(image, mountpoint, read_write=True)
    descriptor: int | None = None
    try:
        descriptor = os.open(mountpoint / "Docs" / "ReadMe", os.O_WRONLY | os.O_TRUNC)
        os.write(descriptor, b"flushed during graceful shutdown")
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=30) == 0
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                if exc.errno not in {errno.ENOTCONN, errno.EIO}:
                    raise
        _force_detach(process, mountpoint)

    assert pending_recovery(image) is None
    assert validate_image_report(image).findings == ()
    with AmigaImage.open(image) as reopened:
        readme = reopened.node_at_path("Docs/ReadMe")
        assert reopened.read(readme.inode, 0, 1024) == b"flushed during graceful shutdown"


@live
def test_live_second_mount_of_a_writable_image_is_refused(tmp_path: Path) -> None:
    image = create_floppy(tmp_path)
    with mounted(image, tmp_path / "first", read_write=True):
        second = tmp_path / "second"
        second.mkdir()
        result = subprocess.run(
            [sys.executable, "-m", "amigafs.cli", "mount", str(image), str(second)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 2
        assert "another AmigaFS process" in result.stderr
