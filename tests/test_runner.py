from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from amigafs.errors import AmigaFSError
from amigafs.fuse_adapter.runner import _contains_keyboard_interrupt, mount_image


def test_finds_keyboard_interrupt_inside_exception_group() -> None:
    error = BaseExceptionGroup("Trio nursery", [RuntimeError("other"), KeyboardInterrupt()])
    assert _contains_keyboard_interrupt(error)


def test_rejects_group_without_keyboard_interrupt() -> None:
    error = ExceptionGroup("ordinary failures", [RuntimeError("one"), ValueError("two")])
    assert not _contains_keyboard_interrupt(error)


def _image(order: list[str], *, needs_write_back: bool = False) -> MagicMock:
    image = MagicMock()
    image.source = SimpleNamespace(name="Work bench.adf", primary_path=Path("/images/wb.adf"))
    image.needs_write_back = needs_write_back
    image.close.side_effect = lambda **_kwargs: order.append("image-close")
    return image


def test_mountpoint_must_be_an_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(AmigaFSError, match="does not exist or is not a directory"):
        mount_image("/images/wb.adf", tmp_path / "missing")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "file").write_text("x", encoding="utf-8")
    with (
        patch("amigafs.fuse_adapter.runner.AmigaImage.open") as opened,
        pytest.raises(AmigaFSError, match="must be empty"),
    ):
        mount_image("/images/wb.adf", occupied)
    opened.assert_not_called()


def test_mount_registration_spans_fuse_and_image_shutdown(tmp_path: Path) -> None:
    mountpoint = tmp_path / "mount"
    mountpoint.mkdir()
    order: list[str] = []
    image = _image(order, needs_write_back=True)
    started: list[str] = []
    with (
        patch("amigafs.fuse_adapter.runner.AmigaImage.open", return_value=image) as opened,
        patch("amigafs.fuse_adapter.runner.AmigaOperations") as operations_class,
        patch(
            "amigafs.fuse_adapter.runner.pyfuse3.init",
            side_effect=lambda *_args: order.append("init"),
        ) as init,
        patch(
            "amigafs.fuse_adapter.runner.pyfuse3.close",
            side_effect=lambda *_args, **_kwargs: order.append("close"),
        ),
        patch("amigafs.fuse_adapter.runner.trio.run"),
        patch(
            "amigafs.fuse_adapter.runner.register_mount",
            side_effect=lambda *_args, **_kwargs: order.append("register"),
        ) as register,
        patch(
            "amigafs.fuse_adapter.runner.unregister_mount",
            side_effect=lambda *_args: order.append("unregister"),
        ) as unregister,
    ):
        operations_class.return_value.flush_pending.side_effect = lambda: order.append("flush")
        mount_image(
            "/images/wb.adf",
            mountpoint,
            read_write=True,
            write_back_started=lambda name: started.append(name),
        )

    opened.assert_called_once_with("/images/wb.adf", writable=True, progress=None)
    register.assert_called_once_with(image.source, mountpoint.resolve(), read_write=True)
    # The record outlives the mount until the image has been written back and closed.
    assert order == ["register", "init", "flush", "close", "image-close", "unregister"]
    image.close.assert_called_once_with(clean=True, progress=None)
    assert started == ["Work bench.adf"]
    options = init.call_args.args[2]
    assert {"nodev", "nosuid", "noexec", "subtype=amigafs", "fsname=Work_bench.adf"} <= options
    assert "ro" not in options
    unregister.assert_called_once_with(mountpoint.resolve())


def test_read_only_mount_is_mounted_read_only(tmp_path: Path) -> None:
    mountpoint = tmp_path / "mount"
    mountpoint.mkdir()
    image = _image([])
    with (
        patch("amigafs.fuse_adapter.runner.AmigaImage.open", return_value=image),
        patch("amigafs.fuse_adapter.runner.AmigaOperations"),
        patch("amigafs.fuse_adapter.runner.pyfuse3.init") as init,
        patch("amigafs.fuse_adapter.runner.pyfuse3.close"),
        patch("amigafs.fuse_adapter.runner.trio.run"),
        patch("amigafs.fuse_adapter.runner.register_mount"),
        patch("amigafs.fuse_adapter.runner.unregister_mount"),
    ):
        mount_image("/images/wb.adf", mountpoint)
    assert "ro" in init.call_args.args[2]


def test_failed_shutdown_flush_detaches_and_closes_the_image_unclean(tmp_path: Path) -> None:
    mountpoint = tmp_path / "mount"
    mountpoint.mkdir()
    image = _image([], needs_write_back=True)
    operations = MagicMock()
    operations.flush_pending.side_effect = AmigaFSError("injected dirty-buffer failure")
    started: list[str] = []

    with (
        patch("amigafs.fuse_adapter.runner.AmigaImage.open", return_value=image),
        patch("amigafs.fuse_adapter.runner.AmigaOperations", return_value=operations),
        patch("amigafs.fuse_adapter.runner.pyfuse3.init"),
        patch("amigafs.fuse_adapter.runner.pyfuse3.close") as close,
        patch("amigafs.fuse_adapter.runner.trio.run"),
        patch("amigafs.fuse_adapter.runner.register_mount"),
        patch("amigafs.fuse_adapter.runner.unregister_mount") as unregister,
        pytest.raises(AmigaFSError, match="dirty-buffer failure"),
    ):
        mount_image(
            "/images/wb.adf", mountpoint, read_write=True, write_back_started=started.append
        )

    close.assert_called_once_with()
    # Nothing is written back to the source from a session that did not end cleanly.
    image.close.assert_called_once_with(clean=False, progress=None)
    assert started == []
    unregister.assert_called_once_with(mountpoint.resolve())


def test_failed_fuse_initialisation_closes_the_image_and_its_record(tmp_path: Path) -> None:
    mountpoint = tmp_path / "mount"
    mountpoint.mkdir()
    image = _image([])
    with (
        patch("amigafs.fuse_adapter.runner.AmigaImage.open", return_value=image),
        patch("amigafs.fuse_adapter.runner.AmigaOperations"),
        patch("amigafs.fuse_adapter.runner.pyfuse3.init", side_effect=RuntimeError("no fuse")),
        patch("amigafs.fuse_adapter.runner.register_mount"),
        patch("amigafs.fuse_adapter.runner.unregister_mount") as unregister,
        pytest.raises(RuntimeError, match="no fuse"),
    ):
        mount_image("/images/wb.adf", mountpoint, read_write=True)
    image.close.assert_called_once_with(clean=False, progress=None)
    unregister.assert_called_once_with(mountpoint.resolve())
