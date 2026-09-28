from unittest.mock import patch

from amigafs.diagnostics import diagnostic_report
from amigafs.errors import AmigaFSError
from amigafs.mounts import MountRecord


def test_diagnostics_omit_absolute_image_and_mount_paths() -> None:
    record = MountRecord(
        mountpoint="/home/alice/AmigaFS Mounts/private-disc-123",
        source="private.adf",
        options="rw,nosuid,nodev",
        image_path="/home/alice/secret/client/private.adf",
        image_device=42,
        image_inode=99,
        pid=1234,
        read_write=True,
    )
    with patch("amigafs.diagnostics.active_mounts", return_value=[record]):
        report = diagnostic_report()

    rendered = repr(report)
    assert "/home/alice" not in rendered
    assert "secret/client" not in rendered
    assert report["mounts"][0]["image_name"] == "private.adf"
    assert report["mounts"][0]["image_identity"]
    assert report["mount_location"] == {"mode": "sidebar", "source": "default"}


def test_diagnostics_report_invalid_preferences_without_exposing_details() -> None:
    with (
        patch("amigafs.diagnostics.active_mounts", return_value=[]),
        patch(
            "amigafs.diagnostics.mount_location",
            side_effect=AmigaFSError("/home/alice/private/preferences.json is corrupt"),
        ),
    ):
        report = diagnostic_report()

    assert report["mount_location"] == {
        "mode": "invalid",
        "source": "preferences-error",
    }
    assert "/home/alice" not in repr(report)


def test_diagnostics_sanitise_untrusted_mount_fields() -> None:
    record = MountRecord(
        mountpoint="/mounts/private\nname",
        source="/unrelated/secret/source.adf",
        options="rw,nosuid,credential=/unrelated/token",
        image_path="/images/private.adf",
    )
    with patch("amigafs.diagnostics.active_mounts", return_value=[record]):
        report = diagnostic_report()

    rendered = repr(report)
    assert "/unrelated" not in rendered
    assert "credential" not in rendered
    assert report["mounts"][0]["source_name"] == "source.adf"
    assert report["mounts"][0]["options"] == "rw,nosuid"
