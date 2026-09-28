import os
from unittest.mock import patch

from amigafs.fuse_adapter.availability import fuse_device_accessible, live_fuse_available


def test_fuse_probe_requires_successful_device_open() -> None:
    with patch("amigafs.fuse_adapter.availability.os.open", side_effect=PermissionError):
        assert not fuse_device_accessible()


def test_fuse_probe_closes_successfully_opened_device() -> None:
    with (
        patch("amigafs.fuse_adapter.availability.os.open", return_value=42) as open_device,
        patch("amigafs.fuse_adapter.availability.os.close") as close_device,
        patch("amigafs.fuse_adapter.availability.shutil.which", return_value="/bin/fusermount3"),
    ):
        assert live_fuse_available()
    open_device.assert_called_once_with("/dev/fuse", os.O_RDWR | os.O_NONBLOCK)
    close_device.assert_called_once_with(42)
