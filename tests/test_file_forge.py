from pathlib import Path
from unittest.mock import patch

import pytest

from amigafs.errors import AmigaFSError
from amigafs.file_forge import file_forge_available, file_forge_command, open_in_file_forge
from tests.image_fixture import create_floppy, create_hard_disc, gzip_image


def test_configured_launcher_expands_the_image_placeholder_without_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = create_floppy(tmp_path).rename(tmp_path / "work bench; rm -rf $HOME.adf")
    monkeypatch.setenv("AMIGA_FILE_FORGE_COMMAND", "file-forge-client --image {image} --verbose")

    assert file_forge_command(image_path) == [
        "file-forge-client",
        "--image",
        str(image_path),
        "--verbose",
    ]


def test_configured_launcher_appends_the_image_without_placeholders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = create_hard_disc(tmp_path, capacity="4MB")
    monkeypatch.setenv("AMIGA_FILE_FORGE_COMMAND", "flatpak run example.FileForge")

    assert file_forge_command(image_path) == [
        "flatpak",
        "run",
        "example.FileForge",
        str(image_path),
    ]


def test_every_image_kind_file_forge_opens_is_handed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMIGA_FILE_FORGE_COMMAND", "forge")
    source = create_floppy(tmp_path)
    packed = gzip_image(source, tmp_path / "disk.adz")
    assert file_forge_command(packed) == ["forge", str(packed)]
    link = tmp_path / "link.adf"
    link.symlink_to(source)
    assert file_forge_command(link) == ["forge", str(source.resolve())]


def test_sources_that_are_not_files_are_never_handed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMIGA_FILE_FORGE_COMMAND", "forge")
    not_amiga = tmp_path / "notes.adf"
    not_amiga.write_bytes(b"just some text")
    with pytest.raises(AmigaFSError):
        file_forge_command(not_amiga)
    with pytest.raises(AmigaFSError):
        file_forge_command(tmp_path / "missing.adf")


def test_missing_native_launcher_is_unavailable_and_explained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = create_floppy(tmp_path)
    monkeypatch.delenv("AMIGA_FILE_FORGE_COMMAND", raising=False)
    monkeypatch.setattr("amigafs.file_forge.shutil.which", lambda _name: None)
    monkeypatch.setattr("amigafs.file_forge.Path.home", lambda: tmp_path)

    assert not file_forge_available()
    with pytest.raises(AmigaFSError, match="native Amiga File Forge application"):
        file_forge_command(image_path)


def test_native_user_launcher_is_detected_when_nautilus_path_omits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = tmp_path / ".local/bin/amiga-file-forge"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    image_path = create_floppy(tmp_path)
    monkeypatch.delenv("AMIGA_FILE_FORGE_COMMAND", raising=False)
    monkeypatch.setattr("amigafs.file_forge.shutil.which", lambda _name: None)
    monkeypatch.setattr("amigafs.file_forge.Path.home", lambda: tmp_path)

    assert file_forge_available()
    assert file_forge_command(image_path) == [str(launcher), str(image_path)]
    launcher.chmod(0o644)
    assert not file_forge_available()


@pytest.mark.parametrize("configured", ["'unterminated", "   ", "/nonexistent/forge {image}"])
def test_malformed_or_missing_configured_launcher_hides_the_action(
    configured: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMIGA_FILE_FORGE_COMMAND", configured)
    monkeypatch.setattr("amigafs.file_forge.shutil.which", lambda _name: None)
    monkeypatch.setattr("amigafs.file_forge.Path.home", lambda: Path("/nonexistent"))
    assert not file_forge_available()


def test_launch_is_detached_and_shell_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image_path = create_floppy(tmp_path)
    monkeypatch.setenv("AMIGA_FILE_FORGE_COMMAND", "file-forge-client")

    with patch("amigafs.file_forge.subprocess.Popen") as popen:
        open_in_file_forge(image_path)

    popen.assert_called_once()
    assert popen.call_args.args[0] == ["file-forge-client", str(image_path)]
    assert popen.call_args.kwargs["start_new_session"] is True
    assert "shell" not in popen.call_args.kwargs


def test_launch_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image_path = create_floppy(tmp_path)
    monkeypatch.setenv("AMIGA_FILE_FORGE_COMMAND", "file-forge-client")

    with (
        patch("amigafs.file_forge.subprocess.Popen", side_effect=OSError("cannot execute")),
        pytest.raises(AmigaFSError, match="Could not start Amiga File Forge"),
    ):
        open_in_file_forge(image_path)
