from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from amigafs.recovery import RecoveryInfo
from tests.image_fixture import create_floppy, create_hard_disc, gzip_image


class _Menu:
    def __init__(self) -> None:
        self.items: list[_MenuItem] = []

    def append_item(self, item: _MenuItem) -> None:
        self.items.append(item)


class _MenuItem:
    def __init__(self, **values: str) -> None:
        self.name = values["name"]
        self.label = values["label"]
        self.tip = values["tip"]
        self.icon = values["icon"]
        self.submenu: _Menu | None = None
        self.callback: Any = None
        self.arguments: tuple[Any, ...] = ()

    def connect(self, _signal: str, callback: Any, *arguments: Any) -> None:
        self.callback = callback
        self.arguments = arguments

    def set_submenu(self, submenu: _Menu) -> None:
        self.submenu = submenu


class _ListStore:
    def __init__(self) -> None:
        self.items: list[Any] = []

    @classmethod
    def new(cls, **_kwargs: Any) -> _ListStore:
        return cls()

    def append(self, item: Any) -> None:
        self.items.append(item)


class _FileInfo:
    def __init__(self, path: Path, *, directory: bool = False) -> None:
        self.path = path
        self.directory = directory

    def get_uri_scheme(self) -> str:
        return "file"

    def get_location(self) -> Any:
        return SimpleNamespace(get_path=lambda: str(self.path))

    def is_directory(self) -> bool:
        return self.directory


def _load_extension(monkeypatch: Any) -> Any:
    gi = ModuleType("gi")
    gi.require_version = lambda *_args: None  # type: ignore[attr-defined]
    repository = ModuleType("gi.repository")
    repository.Gio = SimpleNamespace(ListStore=_ListStore)
    repository.GObject = SimpleNamespace(GObject=type("GObject", (), {}))
    repository.Nautilus = SimpleNamespace(
        Menu=_Menu,
        MenuItem=_MenuItem,
        MenuProvider=type("MenuProvider", (), {}),
        PropertiesModelProvider=type("PropertiesModelProvider", (), {}),
        PropertiesItem=lambda **values: SimpleNamespace(**values),
        PropertiesModel=lambda **values: SimpleNamespace(**values),
    )
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    sys.modules.pop("amigafs_nautilus.extension", None)
    extension = importlib.import_module("amigafs_nautilus.extension")
    monkeypatch.setattr(extension, "physical_write_available", lambda _path: False)
    monkeypatch.setattr(extension, "floppy_drive_available", lambda: False)
    monkeypatch.setattr(extension, "list_discs", lambda: [])
    monkeypatch.setattr(extension, "pending_physical_recoveries", lambda: ())
    monkeypatch.setattr(extension, "file_forge_available", lambda: True)
    return extension


def _actions(items: list[Any]) -> dict[str, _MenuItem]:
    assert len(items) == 1
    parent = items[0]
    assert parent.name == "AmigaFS::Support"
    assert parent.label == "Amiga FS Support"
    assert parent.submenu is not None
    return {item.name.removeprefix("AmigaFS::"): item for item in parent.submenu.items}


def _launched(extension: Any, monkeypatch: Any) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(extension, "_launch", lambda *arguments: calls.append(arguments))
    return calls


def test_image_actions_are_collapsed_under_one_support_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    image = create_floppy(tmp_path)
    actions = _actions(extension.AmigaFSMenuProvider().get_file_items([_FileInfo(image)]))
    assert list(actions) == [
        "MountReadOnly",
        "MountReadWrite",
        "Validate",
        "Repair",
        "OpenFileForge",
        "ConfigureMountLocation",
    ]
    for item in actions.values():
        assert item.label and item.tip and item.icon.endswith("-symbolic")


def test_selection_of_several_files_or_a_remote_file_offers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    image = _FileInfo(create_floppy(tmp_path))
    provider = extension.AmigaFSMenuProvider()
    assert provider.get_file_items([image, image]) == []
    remote = _FileInfo(tmp_path / "remote.adf")
    remote.get_uri_scheme = lambda: "sftp"  # type: ignore[method-assign]
    assert provider.get_file_items([remote]) == []


def test_image_actions_launch_shell_free_desktop_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    image = create_floppy(tmp_path).rename(tmp_path / "odd name; $(reboot).adf")
    monkeypatch.setattr(extension, "physical_write_available", lambda _path: True)
    calls = _launched(extension, monkeypatch)
    actions = _actions(extension.AmigaFSMenuProvider().get_file_items([_FileInfo(image)]))
    for name in (
        "MountReadOnly",
        "MountReadWrite",
        "Validate",
        "Repair",
        "WritePhysicalFloppy",
        "OpenFileForge",
        "ConfigureMountLocation",
    ):
        actions[name].callback(actions[name])
    assert calls == [
        ("desktop-mount", str(image)),
        ("desktop-mount", "--read-write", str(image)),
        ("desktop-validate", str(image)),
        ("desktop-repair", str(image)),
        ("desktop-write-floppy", str(image)),
        ("desktop-open-file-forge", str(image)),
        ("desktop-configure-mount-location",),
    ]


def test_read_only_and_container_formats_hide_actions_they_cannot_perform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    packed = gzip_image(create_floppy(tmp_path), tmp_path / "disk.adz")
    actions = _actions(extension.AmigaFSMenuProvider().get_file_items([_FileInfo(packed)]))
    assert "MountReadWrite" in actions and "Repair" not in actions
    archive = tmp_path / "game.dms"
    archive.write_bytes(b"DMS!" + bytes(64))
    actions = _actions(extension.AmigaFSMenuProvider().get_file_items([_FileInfo(archive)]))
    assert "MountReadOnly" in actions
    assert "MountReadWrite" not in actions and "Repair" not in actions


def test_file_forge_action_is_hidden_when_native_app_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    monkeypatch.setattr(extension, "file_forge_available", lambda: False)
    image = create_floppy(tmp_path)
    actions = _actions(extension.AmigaFSMenuProvider().get_file_items([_FileInfo(image)]))
    assert "OpenFileForge" not in actions


def test_physical_floppy_write_is_offered_only_when_greaseweazle_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    floppy = create_floppy(tmp_path)
    hard_disc = create_hard_disc(tmp_path, capacity="4MB")
    provider = extension.AmigaFSMenuProvider()
    assert "WritePhysicalFloppy" not in _actions(provider.get_file_items([_FileInfo(floppy)]))
    monkeypatch.setattr(extension, "physical_write_available", lambda _path: True)
    assert "WritePhysicalFloppy" in _actions(provider.get_file_items([_FileInfo(floppy)]))
    # A hard-disc image is never offered to a floppy drive.
    assert "WritePhysicalFloppy" not in _actions(provider.get_file_items([_FileInfo(hard_disc)]))


def test_protected_flux_image_that_cannot_be_mounted_can_still_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    monkeypatch.setattr(extension, "physical_write_available", lambda _path: True)
    monkeypatch.setattr(extension, "menu_capabilities", lambda _path: None)
    flux = tmp_path / "protected.ipf"
    flux.write_bytes(b"CAPS" + bytes(64))
    actions = _actions(extension.AmigaFSMenuProvider().get_file_items([_FileInfo(flux)]))
    assert list(actions) == ["WritePhysicalFloppy"]


def test_physical_disc_write_is_offered_for_hard_disc_images_when_a_disc_is_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    hard_disc = create_hard_disc(tmp_path, capacity="4MB")
    provider = extension.AmigaFSMenuProvider()
    assert "WritePhysicalDisc" not in _actions(provider.get_file_items([_FileInfo(hard_disc)]))
    monkeypatch.setattr(extension, "list_discs", lambda: [object()])
    calls = _launched(extension, monkeypatch)
    actions = _actions(provider.get_file_items([_FileInfo(hard_disc)]))
    actions["WritePhysicalDisc"].callback(None)
    assert calls == [("desktop-write-disc", str(hard_disc))]
    floppy = create_floppy(tmp_path)
    assert "WritePhysicalDisc" not in _actions(provider.get_file_items([_FileInfo(floppy)]))


def test_mounted_image_offers_unmount_and_keeps_its_hand_off_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    image = create_floppy(tmp_path)
    mountpoint = tmp_path / "mounted"
    monkeypatch.setattr(
        extension, "mount_for_image_path", lambda _path: SimpleNamespace(mountpoint=str(mountpoint))
    )
    calls = _launched(extension, monkeypatch)
    actions = _actions(extension.AmigaFSMenuProvider().get_file_items([_FileInfo(image)]))
    assert list(actions) == ["Unmount", "OpenFileForge", "ConfigureMountLocation"]
    actions["Unmount"].callback(None)
    assert calls == [("desktop-unmount", str(mountpoint))]


def test_interrupted_image_offers_recovery_but_no_writable_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    image = create_floppy(tmp_path)
    monkeypatch.setattr(extension, "pending_recovery", lambda _path: object())
    calls = _launched(extension, monkeypatch)
    actions = _actions(extension.AmigaFSMenuProvider().get_file_items([_FileInfo(image)]))
    assert list(actions) == [
        "Recover",
        "MountReadOnly",
        "Validate",
        "OpenFileForge",
        "ConfigureMountLocation",
    ]
    actions["Recover"].callback(None)
    assert calls == [("desktop-recover", str(image))]


def test_writable_folder_offers_image_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    calls = _launched(extension, monkeypatch)
    provider = extension.AmigaFSMenuProvider()
    for items in (
        provider.get_file_items([_FileInfo(tmp_path, directory=True)]),
        provider.get_background_items(_FileInfo(tmp_path, directory=True)),
    ):
        actions = _actions(items)
        assert list(actions) == ["CreateFloppy", "CreateHardDisc", "ConfigureMountLocation"]
    actions["CreateFloppy"].callback(None)
    actions["CreateHardDisc"].callback(None)
    assert calls == [
        ("desktop-create", "--kind", "floppy", str(tmp_path)),
        ("desktop-create", "--kind", "hard-disc", str(tmp_path)),
    ]
    read_only = tmp_path / "read-only"
    read_only.mkdir(mode=0o555)
    import os

    if os.geteuid() != 0:
        assert provider.get_background_items(_FileInfo(read_only, directory=True)) == []


def test_physical_media_are_reached_from_the_folder_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    monkeypatch.setattr(extension, "floppy_drive_available", lambda: True)
    monkeypatch.setattr(extension, "list_discs", lambda: [object()])
    calls = _launched(extension, monkeypatch)
    actions = _actions(
        extension.AmigaFSMenuProvider().get_background_items(_FileInfo(tmp_path, directory=True))
    )
    assert list(actions) == [
        "CreateFloppy",
        "CreateHardDisc",
        "MountFloppyReadOnly",
        "MountFloppyReadWrite",
        "ReadFloppy",
        "MountDiscReadOnly",
        "MountDiscReadWrite",
        "ReadDisc",
        "ConfigureMountLocation",
    ]
    for name in (
        "MountFloppyReadOnly",
        "MountFloppyReadWrite",
        "ReadFloppy",
        "MountDiscReadOnly",
        "MountDiscReadWrite",
        "ReadDisc",
    ):
        actions[name].callback(None)
    assert calls == [
        ("desktop-mount-floppy",),
        ("desktop-mount-floppy", "--read-write"),
        ("desktop-read-floppy", str(tmp_path)),
        ("desktop-mount-disc",),
        ("desktop-mount-disc", "--read-write"),
        ("desktop-read-disc", str(tmp_path)),
    ]


def test_interrupted_physical_sessions_are_listed_in_the_folder_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    pending = RecoveryInfo(
        version=1,
        identity="ab" * 32,
        image_path="/run/user/1000/amigafs/floppy/drive-A",
        kind="workspace",
        created_at="now",
        state="ready",
        size=901120,
        is_device=False,
        detail="greaseweazle",
    )
    monkeypatch.setattr(extension, "pending_physical_recoveries", lambda: (pending,))
    calls = _launched(extension, monkeypatch)
    actions = _actions(
        extension.AmigaFSMenuProvider().get_background_items(_FileInfo(tmp_path, directory=True))
    )
    recovery = actions["RecoverPhysical" + "ab" * 6]
    assert recovery.label == "Resolve interrupted session on drive-A…"
    recovery.callback(None)
    assert calls == [("desktop-recover", pending.image_path)]


def test_mounted_folder_offers_only_unmount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    monkeypatch.setattr(extension, "is_mounted", lambda _path: True)
    monkeypatch.setattr(extension, "floppy_drive_available", lambda: True)
    actions = _actions(
        extension.AmigaFSMenuProvider().get_background_items(_FileInfo(tmp_path, directory=True))
    )
    assert list(actions) == ["Unmount", "ConfigureMountLocation"]


def test_ordinary_file_menu_does_not_run_content_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    document = tmp_path / "notes.txt"
    document.write_text("plain", encoding="utf-8")

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("an ordinary file must not be inspected")

    monkeypatch.setattr("amigafs_nautilus.logic.resolve_image", forbidden)
    monkeypatch.setattr(extension, "mount_for_image_path", forbidden)
    monkeypatch.setattr(extension, "pending_recovery", forbidden)
    assert extension.AmigaFSMenuProvider().get_file_items([_FileInfo(document)]) == []


def test_image_properties_page_describes_the_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    monkeypatch.setattr(extension, "mount_for_image", lambda _source: None)
    image = create_hard_disc(tmp_path, capacity="4MB")
    (model,) = extension.AmigaFSPropertiesModelProvider().get_models([_FileInfo(image)])
    assert model.title == "Amiga disk image"
    rows = {item.name: item.value for item in model.model.items}
    assert rows["Image type"] == "Amiga hard-disc image"
    assert rows["DH1: Volume name"] == "System1"
    assert rows["Mount state"] == "Not mounted"


def test_mounted_or_large_images_are_summarised_without_being_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = _load_extension(monkeypatch)
    image = create_floppy(tmp_path)
    monkeypatch.setattr(extension, "mount_for_image", lambda _source: object())

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a mounted image is locked and must not be opened")

    monkeypatch.setattr(extension, "read_image_properties", forbidden)
    (model,) = extension.AmigaFSPropertiesModelProvider().get_models([_FileInfo(image)])
    rows = {item.name: item.value for item in model.model.items}
    assert rows["Mount state"] == "Mounted"
    assert rows["Image type"] == "Amiga floppy image (ADF)"


def test_properties_failure_is_shown_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amigafs.errors import AmigaFSError

    extension = _load_extension(monkeypatch)
    monkeypatch.setattr(extension, "mount_for_image", lambda _source: None)

    def failing(_source: Any) -> None:
        raise AmigaFSError("the image is damaged")

    monkeypatch.setattr(extension, "read_image_properties", failing)
    (model,) = extension.AmigaFSPropertiesModelProvider().get_models(
        [_FileInfo(create_floppy(tmp_path))]
    )
    assert [(item.name, item.value) for item in model.model.items] == [
        ("Status", "Unavailable: the image is damaged")
    ]


def test_mounted_entry_properties_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extension = _load_extension(monkeypatch)
    entry = tmp_path / "List"
    entry.write_bytes(b"")
    monkeypatch.setattr(
        extension,
        "mounted_file_property_rows",
        lambda _path: (("Source filesystem", "FFS"), ("Protection bits", "----rwed")),
    )
    (model,) = extension.AmigaFSPropertiesModelProvider().get_models([_FileInfo(entry)])
    assert model.title == "Amiga metadata"
    monkeypatch.setattr(extension, "mounted_file_property_rows", lambda _path: ())
    assert extension.AmigaFSPropertiesModelProvider().get_models([_FileInfo(entry)]) == []
