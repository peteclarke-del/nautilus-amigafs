"""Nautilus 4 context-menu integration for AmigaFS."""

from __future__ import annotations

import os
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

import gi

from amigafs.core import read_image_properties, resolve_image
from amigafs.core.devices import list_discs
from amigafs.errors import AmigaFSError
from amigafs.file_forge import file_forge_available
from amigafs.greaseweazle import floppy_drive_available, physical_write_available
from amigafs.i18n import _
from amigafs.mounts import is_mounted, mount_for_image, mount_for_image_path
from amigafs.recovery import pending_physical_recoveries, pending_recovery
from amigafs_nautilus.logic import (
    decodes_quickly,
    image_property_rows,
    is_supported_image,
    menu_capabilities,
    mounted_file_property_rows,
    summary_property_rows,
)

gi.require_version("Nautilus", "4.0")
from gi.repository import Gio, GObject, Nautilus  # noqa: E402

_COMMAND = ["amigafs"]


def configure_command(command: list[str]) -> None:
    global _COMMAND
    _COMMAND = command


def _local_path(file_info: Any) -> Path | None:
    """Return where the file can be opened by name, or ``None`` when it cannot.

    A file on a network share has a scheme such as ``smb`` but is still
    reachable by name where the desktop mounts the share, so the scheme does
    not decide. A location with no such name has no path.
    """

    location = file_info.get_location()
    path = location.get_path() if location is not None else None
    return Path(path) if path else None


def _launch(*arguments: str) -> None:
    subprocess.Popen(
        [*_COMMAND, *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class AmigaFSMenuProvider(GObject.GObject, Nautilus.MenuProvider):
    """Add mount lifecycle actions for Amiga images, discs and floppies."""

    @staticmethod
    def _support_menu(items: list[Any]) -> Any:
        submenu = Nautilus.Menu()
        for item in items:
            submenu.append_item(item)
        parent = Nautilus.MenuItem(
            name="AmigaFS::Support",
            label=_("Amiga FS Support"),
            tip=_("Open AmigaFS image and filesystem actions"),
            icon="drive-harddisk-symbolic",
        )
        parent.set_submenu(submenu)
        return parent

    @staticmethod
    def _item(name: str, label: str, tip: str, icon: str, *arguments: str) -> Any:
        item = Nautilus.MenuItem(name=f"AmigaFS::{name}", label=label, tip=tip, icon=icon)
        item.connect("activate", lambda _menu: _launch(*arguments))
        return item

    def _configuration_item(self) -> Any:
        return self._item(
            "ConfigureMountLocation",
            _("Mount location…"),
            _("Choose where future AmigaFS desktop mounts appear"),
            "preferences-system-symbolic",
            "desktop-configure-mount-location",
        )

    def _create_items(self, directory: Path) -> list[Any]:
        return [
            self._item(
                "CreateFloppy",
                _("Create floppy image…"),
                _("Create an empty ADF in {directory}").format(directory=directory.name),
                "document-new-symbolic",
                "desktop-create",
                "--kind",
                "floppy",
                str(directory),
            ),
            self._item(
                "CreateHardDisc",
                _("Create hard-disc image…"),
                _("Create an empty partitioned HDF in {directory}").format(
                    directory=directory.name
                ),
                "document-new-symbolic",
                "desktop-create",
                "--kind",
                "hard-disc",
                str(directory),
            ),
        ]

    def _physical_items(self, directory: Path, *, can_create: bool) -> list[Any]:
        """Actions on sources that have no file of their own to right-click."""

        items: list[Any] = []
        if floppy_drive_available():
            items.append(
                self._item(
                    "MountFloppyReadOnly",
                    _("Open physical floppy read-only…"),
                    _("Read the floppy in a Greaseweazle drive and open it"),
                    "media-floppy-symbolic",
                    "desktop-mount-floppy",
                )
            )
            items.append(
                self._item(
                    "MountFloppyReadWrite",
                    _("Open physical floppy read-write…"),
                    _("Open a floppy and write changed tracks back when it is unmounted"),
                    "media-floppy-symbolic",
                    "desktop-mount-floppy",
                    "--read-write",
                )
            )
            if can_create:
                items.append(
                    self._item(
                        "ReadFloppy",
                        _("Read physical floppy to image…"),
                        _(
                            "Save the floppy in a Greaseweazle drive as an ADF in {directory}"
                        ).format(directory=directory.name),
                        "document-save-symbolic",
                        "desktop-read-floppy",
                        str(directory),
                    )
                )
        discs = []
        with suppress(OSError):
            discs = list_discs()
        if discs:
            items.append(
                self._item(
                    "MountDiscReadOnly",
                    _("Open physical Amiga disc read-only…"),
                    _("Open an Amiga hard disc or memory card attached to this computer"),
                    "drive-removable-media-symbolic",
                    "desktop-mount-disc",
                )
            )
            items.append(
                self._item(
                    "MountDiscReadWrite",
                    _("Open physical Amiga disc read-write…"),
                    _("Open an attached Amiga disc with an undo journal"),
                    "drive-removable-media-symbolic",
                    "desktop-mount-disc",
                    "--read-write",
                )
            )
            if can_create:
                items.append(
                    self._item(
                        "ReadDisc",
                        _("Read physical disc to image…"),
                        _("Copy a whole attached disc to an image in {directory}").format(
                            directory=directory.name
                        ),
                        "document-save-symbolic",
                        "desktop-read-disc",
                        str(directory),
                    )
                )
        for info in pending_physical_recoveries():
            name = Path(info.image_path).name
            items.append(
                self._item(
                    f"RecoverPhysical{info.identity[:12]}",
                    _("Resolve interrupted session on {name}…").format(name=name),
                    _("Restore, keep or salvage the changes of an interrupted session"),
                    "document-revert-symbolic",
                    "desktop-recover",
                    info.image_path,
                )
            )
        return items

    @staticmethod
    def _can_create_in(directory: Path) -> bool:
        return directory.is_dir() and os.access(directory, os.W_OK | os.X_OK)

    def _validate_item(self, path: Path) -> Any:
        return self._item(
            "Validate",
            _("Validate image"),
            _("Check {image} without modifying it").format(image=path.name),
            "emblem-default-symbolic",
            "desktop-validate",
            str(path),
        )

    def _read_only_item(self, path: Path) -> Any:
        return self._item(
            "MountReadOnly",
            _("Open read-only"),
            _("Mount {image} without allowing changes").format(image=path.name),
            "changes-prevent-symbolic",
            "desktop-mount",
            str(path),
        )

    def _read_write_item(self, path: Path) -> Any:
        return self._item(
            "MountReadWrite",
            _("Open read-write"),
            _("Mount {image} read-write with a recovery checkpoint").format(image=path.name),
            "drive-harddisk-symbolic",
            "desktop-mount",
            "--read-write",
            str(path),
        )

    def _repair_item(self, path: Path) -> Any:
        return self._item(
            "Repair",
            _("Repair image…"),
            _("Review eligible low-risk repairs for {image}").format(image=path.name),
            "document-edit-symbolic",
            "desktop-repair",
            str(path),
        )

    def _unmount_item(self, mountpoint: Path) -> Any:
        return self._item(
            "Unmount",
            _("Unmount"),
            _("Unmount {mountpoint}").format(mountpoint=mountpoint.name),
            "media-eject-symbolic",
            "desktop-unmount",
            str(mountpoint),
        )

    def _file_forge_item(self, path: Path) -> Any:
        return self._item(
            "OpenFileForge",
            _("Open in Amiga File Forge…"),
            _("Open {image} in Amiga File Forge").format(image=path.name),
            "document-open-symbolic",
            "desktop-open-file-forge",
            str(path),
        )

    def _write_floppy_item(self, path: Path) -> Any:
        return self._item(
            "WritePhysicalFloppy",
            _("Write to physical floppy…"),
            _("Write and verify {image} using Greaseweazle").format(image=path.name),
            "media-floppy-symbolic",
            "desktop-write-floppy",
            str(path),
        )

    def _write_disc_item(self, path: Path) -> Any:
        return self._item(
            "WritePhysicalDisc",
            _("Write to physical disc…"),
            _("Replace an attached disc with {image} and verify it").format(image=path.name),
            "drive-removable-media-symbolic",
            "desktop-write-disc",
            str(path),
        )

    def _folder_items(self, path: Path) -> list[Any]:
        if is_mounted(path):
            return [self._support_menu([self._unmount_item(path), self._configuration_item()])]
        can_create = self._can_create_in(path)
        items = self._create_items(path) if can_create else []
        items.extend(self._physical_items(path, can_create=can_create))
        if not items:
            return []
        items.append(self._configuration_item())
        return [self._support_menu(items)]

    def get_file_items(self, files: list[Any]) -> list[Any]:
        if len(files) != 1:
            return []
        file_info = files[0]
        path = _local_path(file_info)
        if path is None:
            return []
        if file_info.is_directory():
            return self._folder_items(path)
        capabilities = menu_capabilities(path)
        offer_physical_write = physical_write_available(path)
        if capabilities is None:
            if offer_physical_write:
                return [self._support_menu([self._write_floppy_item(path)])]
            return []
        offer_physical_write = offer_physical_write and capabilities.write_floppy
        offer_file_forge = capabilities.file_forge and file_forge_available()
        offer_disc_write = False
        if path.suffix.casefold() in {".hdf", ".hda", ".rdsk"}:
            with suppress(OSError):
                offer_disc_write = bool(list_discs())
        try:
            mounted = mount_for_image_path(path)
        except AmigaFSError:
            return []
        extras: list[Any] = []
        if offer_physical_write:
            extras.append(self._write_floppy_item(path))
        if offer_file_forge:
            extras.append(self._file_forge_item(path))
        if mounted is not None:
            return [
                self._support_menu(
                    [
                        self._unmount_item(Path(mounted.mountpoint)),
                        *extras,
                        self._configuration_item(),
                    ]
                )
            ]
        recovery = None
        if capabilities.recover:
            with suppress(AmigaFSError):
                recovery = pending_recovery(path)
        if recovery is not None:
            recovery_items = [
                self._item(
                    "Recover",
                    _("Resolve interrupted read-write mount…"),
                    _("Restore the pre-mount checkpoint or keep the current image"),
                    "document-revert-symbolic",
                    "desktop-recover",
                    str(path),
                )
            ]
            if capabilities.mount_read_only:
                recovery_items.append(self._read_only_item(path))
            if capabilities.validate:
                recovery_items.append(self._validate_item(path))
            recovery_items.extend(extras)
            recovery_items.append(self._configuration_item())
            return [self._support_menu(recovery_items)]
        image_items: list[Any] = []
        if capabilities.mount_read_only:
            image_items.append(self._read_only_item(path))
        if capabilities.mount_read_write:
            image_items.append(self._read_write_item(path))
        if capabilities.validate:
            image_items.append(self._validate_item(path))
        if capabilities.repair:
            image_items.append(self._repair_item(path))
        if offer_disc_write:
            image_items.append(self._write_disc_item(path))
        image_items.extend(extras)
        image_items.append(self._configuration_item())
        return [self._support_menu(image_items)]

    def get_background_items(self, current_folder: Any) -> list[Any]:
        path = _local_path(current_folder)
        if path is None:
            return []
        return self._folder_items(path)


class AmigaFSPropertiesModelProvider(GObject.GObject, Nautilus.PropertiesModelProvider):
    """Show image compatibility and mounted-entry Amiga metadata."""

    @staticmethod
    def _model(title: str, rows: tuple[tuple[str, str], ...]) -> Any:
        items = Gio.ListStore.new(item_type=Nautilus.PropertiesItem)
        for name, value in rows:
            items.append(Nautilus.PropertiesItem(name=name, value=value))
        return Nautilus.PropertiesModel(title=title, model=items)

    def get_models(self, files: list[Any]) -> list[Any]:
        if len(files) != 1:
            return []
        path = _local_path(files[0])
        if path is None:
            return []
        if is_supported_image(path):
            try:
                source = resolve_image(path)
                mounted = mount_for_image(source) is not None
                mount_state = _("Mounted") if mounted else _("Not mounted")
                if mounted or not decodes_quickly(source):
                    # A mounted image is locked by its daemon, and a large
                    # container would take too long to decode here.
                    described = summary_property_rows(source)
                else:
                    described = image_property_rows(read_image_properties(source))
                rows = (*described, (_("Mount state"), mount_state))
            except AmigaFSError as exc:
                rows = ((_("Status"), _("Unavailable: {error}").format(error=exc)),)
            return [self._model(_("Amiga disk image"), rows)]
        rows = mounted_file_property_rows(path)
        return [self._model(_("Amiga metadata"), rows)] if rows else []
