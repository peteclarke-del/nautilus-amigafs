"""Persistent per-user AmigaFS preferences."""

from __future__ import annotations

import json
import os
import pwd
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from amigafs.errors import AmigaFSError
from amigafs.i18n import _
from amigafs.mounts import runtime_root
from amigafs.safe_paths import atomic_write_private_text, ensure_private_directory

CONFIG_VERSION = 1
MOUNT_ROOT_ENV = "AMIGAFS_MOUNT_ROOT"


@dataclass(frozen=True, slots=True)
class MountLocation:
    mode: str
    root: Path
    source: str


def _account_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def config_home() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    return Path(configured).expanduser() if configured else _account_home() / ".config"


def preferences_path() -> Path:
    return config_home() / "amigafs" / "preferences.json"


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parse_location(value: str, *, source: str) -> MountLocation:
    candidate = value.strip()
    if candidate == "sidebar":
        return MountLocation("sidebar", _account_home() / "AmigaFS Mounts", source)
    if candidate == "runtime":
        return MountLocation("runtime", runtime_root() / "images", source)
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        raise AmigaFSError(
            _("The mount location must be 'sidebar', 'runtime', or an absolute path.")
        )
    if path.is_symlink():
        raise AmigaFSError(_("A symbolic link cannot be used as the AmigaFS mount location."))
    path = path.resolve(strict=False)
    if path == Path("/"):
        raise AmigaFSError(_("The filesystem root cannot be used as the AmigaFS mount location."))
    return MountLocation("custom", path, source)


def _read_preferences() -> dict[str, Any]:
    path = preferences_path()
    if not path.exists():
        return {}
    try:
        if path.is_symlink() or path.stat().st_size > 16 * 1024:
            raise AmigaFSError(
                _("The AmigaFS preferences file is unsafe: {path}").format(path=path)
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except AmigaFSError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AmigaFSError(
            _("Could not read AmigaFS preferences: {error}").format(error=exc)
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != CONFIG_VERSION:
        raise AmigaFSError(_("The AmigaFS preferences file has an unsupported format."))
    return payload


def mount_location() -> MountLocation:
    """Resolve the effective mount root, with the environment taking precedence."""

    environment = os.environ.get(MOUNT_ROOT_ENV)
    if environment is not None:
        return _parse_location(environment, source="environment")
    configured = _read_preferences().get("mount_location", "sidebar")
    if not isinstance(configured, str):
        raise AmigaFSError(_("The configured AmigaFS mount location is invalid."))
    source = "default" if configured == "sidebar" and not preferences_path().exists() else "user"
    return _parse_location(configured, source=source)


def mount_root() -> Path:
    return mount_location().root


def ensure_mount_root() -> Path:
    """Create and verify the selected mount root without following its final components."""

    location = mount_location()
    if location.mode == "sidebar":
        anchor = _account_home()
    elif location.mode == "runtime":
        anchor = runtime_root().parent
    else:
        anchor = location.root.parent
    return ensure_private_directory(location.root, anchor=anchor)


def set_mount_location(value: str) -> MountLocation:
    """Validate and atomically persist a per-user mount location."""

    parsed = _parse_location(value, source="user")
    stored = parsed.mode if parsed.mode != "custom" else str(parsed.root)
    path = preferences_path()
    ensure_private_directory(path.parent, anchor=config_home())
    if path.is_symlink():
        raise AmigaFSError(
            _("Refusing to replace a symbolic-link preferences file: {path}").format(path=path)
        )
    payload = {"version": CONFIG_VERSION, "mount_location": stored}
    try:
        content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        atomic_write_private_text(path, content, anchor=config_home())
    except (OSError, MemoryError) as exc:
        raise AmigaFSError(
            _("Could not save AmigaFS preferences: {error}").format(error=exc)
        ) from exc
    return parsed


def reset_mount_location() -> MountLocation:
    """Remove the persisted preference and return the effective default/override."""

    path = preferences_path()
    if path.is_symlink():
        raise AmigaFSError(
            _("Refusing to remove a symbolic-link preferences file: {path}").format(path=path)
        )
    try:
        path.unlink(missing_ok=True)
        if path.parent.is_dir():
            _sync_directory(path.parent)
    except OSError as exc:
        raise AmigaFSError(
            _("Could not reset AmigaFS preferences: {error}").format(error=exc)
        ) from exc
    return mount_location()


__all__ = [
    "MountLocation",
    "config_home",
    "ensure_mount_root",
    "mount_location",
    "mount_root",
    "preferences_path",
    "reset_mount_location",
    "set_mount_location",
]
